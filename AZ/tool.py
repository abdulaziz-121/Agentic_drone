from strands import Agent, tool
from strands.models.openai import OpenAIModel
from strands_tools import retrieve, http_request
import asyncio
import math
import os
import threading
from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan
from dotenv import load_dotenv
import camera as _cam


load_dotenv()
threading.Thread(target=_cam.init, daemon=True).start()
drone = System()
MAX_GOTO_DISTANCE_M = float(os.getenv("PX4_MAX_GOTO_DISTANCE_M", "200"))
MAX_MISSION_DISTANCE_M = float(os.getenv("PX4_MAX_MISSION_DISTANCE_M", "500"))
MAX_ALTITUDE_M = float(os.getenv("PX4_MAX_ALTITUDE_M", "50"))
MAX_SPEED_M_S = float(os.getenv("PX4_MAX_SPEED_M_S", "10"))
DEFAULT_MISSION_ALTITUDE_M = float(os.getenv("PX4_DEFAULT_MISSION_ALTITUDE_M", "5"))
DEFAULT_MISSION_SPEED_M_S = float(os.getenv("PX4_DEFAULT_MISSION_SPEED_M_S", "3"))


model = OpenAIModel(
    client_args={"api_key": os.environ["OPENAI_API_KEY"]},
    model_id="gpt-5.4-mini-2026-03-17",
)


async def read_one(stream, timeout=3):
    async def read():
        async for value in stream:
            return value

    return await asyncio.wait_for(read(), timeout=timeout)


async def read_for_time(stream, seconds=10, interval_s=1):
    seconds = max(1, min(seconds, 60))
    interval_s = max(0.2, min(interval_s, 10))
    end_time = asyncio.get_running_loop().time() + seconds
    values = []

    async for value in stream:
        values.append(str(value))

        if asyncio.get_running_loop().time() >= end_time:
            break

        if len(values) >= 30:
            break

        await asyncio.sleep(interval_s)

    return values

def distance_m(latitude_1, longitude_1, latitude_2, longitude_2):
    earth_radius_m = 6_371_000
    lat_1 = math.radians(latitude_1)
    lat_2 = math.radians(latitude_2)
    delta_lat = math.radians(latitude_2 - latitude_1)
    delta_lon = math.radians(longitude_2 - longitude_1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_1) * math.cos(lat_2) * math.sin(delta_lon / 2) ** 2
    )
    return earth_radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def valid_coordinate(latitude_deg, longitude_deg):
    if latitude_deg == 0 and longitude_deg == 0:
        return False

    return -90 <= latitude_deg <= 90 and -180 <= longitude_deg <= 180


async def current_position():
    return await read_one(drone.telemetry.position())


async def current_health():
    return await read_one(drone.telemetry.health())


async def current_armed():
    return await read_one(drone.telemetry.armed())


async def validate_target_near_vehicle(latitude_deg, longitude_deg, max_distance_m):
    if not valid_coordinate(latitude_deg, longitude_deg):
        return "Rejected unsafe coordinate. Latitude/longitude are invalid or equal to 0,0."

    try:
        position = await current_position()
    except (RuntimeError, asyncio.TimeoutError):
        return "Cannot validate target location because current position is not available. Connect PX4 and wait for position first."

    target_distance_m = distance_m(
        position.latitude_deg,
        position.longitude_deg,
        latitude_deg,
        longitude_deg,
    )

    if target_distance_m > max_distance_m:
        return (
            f"Rejected unsafe target: it is {target_distance_m:.1f} m away, "
            f"but the limit is {max_distance_m:.1f} m."
        )

    return None


def validate_altitude_and_speed(relative_altitude_m=None, speed_m_s=None):
    if relative_altitude_m is not None and not 0 < relative_altitude_m <= MAX_ALTITUDE_M:
        return f"Rejected unsafe altitude. Use altitude between 0 and {MAX_ALTITUDE_M} m."

    if speed_m_s is not None and not 0 < speed_m_s <= MAX_SPEED_M_S:
        return f"Rejected unsafe speed. Use speed between 0 and {MAX_SPEED_M_S} m/s."

    return None


def mission_item(
    latitude_deg,
    longitude_deg,
    relative_altitude_m,
    speed_m_s=5.0,
    loiter_time_s=float("nan"),
    is_fly_through=True,
    yaw_deg=float("nan"),
):
    return MissionItem(
        latitude_deg,
        longitude_deg,
        relative_altitude_m,
        speed_m_s,
        is_fly_through,
        float("nan"),
        float("nan"),
        MissionItem.CameraAction.NONE,
        loiter_time_s,
        float("nan"),
        float("nan"),
        yaw_deg,
        float("nan"),
        MissionItem.VehicleAction.NONE,
    )


@tool
async def connect():
    'If the user ask for connect the px4 this tool helps you to connect it .'

    print("Connecting to PX4...")
    await drone.connect(system_address="udpin://0.0.0.0:14540")
    return "PX4 connection started."


@tool
async def connection_status():
    try:
        async def wait_for_connection():
            async for state in drone.core.connection_state():
                if state.is_connected:
                    print("Drone connected!")
                    return "Drone connected!"

        return await asyncio.wait_for(wait_for_connection(), timeout=3)
    except (RuntimeError, asyncio.TimeoutError):
        return "Drone is not connected."


@tool
async def health_status():
    """Check PX4 health/preflight status."""
    try:
        health = await read_one(drone.telemetry.health())
        return str(health)
    except (RuntimeError, asyncio.TimeoutError):
        return "Health status is not available. Connect to PX4 first."


@tool
async def position_status():
    """Check current drone position."""
    try:
        position = await read_one(drone.telemetry.position())
        return str(position)
    except (RuntimeError, asyncio.TimeoutError):
        return "Position is not available. Connect to PX4 first."


@tool
async def home_position_status():
    """
    Get the drone's home (launch) position — use this for STEP 2 of the incident mission.
    Returns latitude_deg and longitude_deg of the home point.
    If the result shows 0,0 or unavailable, wait a few seconds and call again before proceeding.
    """
    try:
        home = await read_one(drone.telemetry.home())
        lat = home.latitude_deg
        lon = home.longitude_deg
        if lat == 0.0 and lon == 0.0:
            return "Home position is 0,0 — GPS not ready yet. Wait a few seconds and call home_position_status again."
        return f"home_lat={lat:.7f} home_lon={lon:.7f} altitude_m={home.absolute_altitude_m:.1f}"
    except (RuntimeError, asyncio.TimeoutError):
        return "Home position unavailable. Connect to PX4 first."


@tool
async def battery_status():
    """Check current battery status."""
    try:
        battery = await read_one(drone.telemetry.battery())
        return str(battery)
    except (RuntimeError, asyncio.TimeoutError):
        return "Battery status is not available. Connect to PX4 first."


@tool
async def gps_status():
    """Check current GPS status."""
    try:
        gps = await read_one(drone.telemetry.gps_info())
        return str(gps)
    except (RuntimeError, asyncio.TimeoutError):
        return "GPS status is not available. Connect to PX4 first."


@tool
async def flight_mode_status():
    """Check current PX4 flight mode."""
    try:
        flight_mode = await read_one(drone.telemetry.flight_mode())
        return str(flight_mode)
    except (RuntimeError, asyncio.TimeoutError):
        return "Flight mode is not available. Connect to PX4 first."


@tool
async def in_air_status():
    """Check whether the drone is in the air."""
    try:
        in_air = await read_one(drone.telemetry.in_air())
        return f"In air: {in_air}"
    except (RuntimeError, asyncio.TimeoutError):
        return "In-air status is not available. Connect to PX4 first."


@tool
async def Arming():
    try:
        health = await current_health()
    except (RuntimeError, asyncio.TimeoutError):
        return "Rejected arm command. Health status is not available."

    if not health.is_armable:
        return f"Rejected arm command. Vehicle is not armable: {health}"

    await drone.action.arm()
    return "Arm command sent."


@tool
async def disarm():
    """Disarm the drone."""
    await drone.action.disarm()
    return "Disarm command sent."


@tool
async def arming_status():
    try:
        armed = await read_one(drone.telemetry.armed())
        return f"Armed: {armed}"
    except (RuntimeError, asyncio.TimeoutError):
        return "Drone is not armed."


@tool
async def attitude_status():
    """Check current attitude in Euler angles."""
    try:
        attitude = await read_one(drone.telemetry.attitude_euler())
        return str(attitude)
    except (RuntimeError, asyncio.TimeoutError):
        return "Attitude is not available. Connect to PX4 first."


@tool
async def landed_state_status():
    """Check current landed state."""
    try:
        landed_state = await read_one(drone.telemetry.landed_state())
        return str(landed_state)
    except (RuntimeError, asyncio.TimeoutError):
        return "Landed state is not available. Connect to PX4 first."


@tool
async def full_status():
    """Check the most useful PX4 statuses together."""
    results = {
        "connection": await connection_status(),
        "health": await health_status(),
        "armed": await arming_status(),
        "in_air": await in_air_status(),
        "flight_mode": await flight_mode_status(),
        "position": await position_status(),
        "battery": await battery_status(),
        "gps": await gps_status(),
        "mission_progress": await mission_progress_status(),
        "mission_finished": await mission_finished_status(),
    }

    return "\n".join(f"{key}: {value}" for key, value in results.items())


@tool
async def monitor_status(status_name: str, seconds: int = 10, interval_s: float = 1.0):
    """Monitor a status stream over time. status_name can be position, battery, gps, flight_mode, in_air, armed, health, attitude, or mission_progress."""
    try:
        if status_name == "position":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.position(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "battery":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.battery(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "gps":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.gps_info(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "flight_mode":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.flight_mode(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "in_air":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.in_air(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "armed":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.armed(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "health":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.health(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "attitude":
            values = await asyncio.wait_for(read_for_time(drone.telemetry.attitude_euler(), seconds, interval_s), timeout=seconds + 5)
        elif status_name == "mission_progress":
            values = await asyncio.wait_for(read_for_time(drone.mission.mission_progress(), seconds, interval_s), timeout=seconds + 5)
        else:
            return "Unknown status_name. Use position, battery, gps, flight_mode, in_air, armed, health, attitude, or mission_progress."

        if not values:
            return f"No {status_name} updates received."

        return "\n".join(values)
    except (RuntimeError, asyncio.TimeoutError):
        return f"{status_name} monitoring is not available. Connect to PX4 first."


@tool
async def set_takeoff_altitude(altitude_m: float):
    """Set takeoff altitude in meters."""
    validation_error = validate_altitude_and_speed(relative_altitude_m=altitude_m)
    if validation_error:
        return validation_error

    await drone.action.set_takeoff_altitude(altitude_m)
    return f"Takeoff altitude set to {altitude_m} m."


@tool
async def takeoff():
    """Command the drone to take off."""
    try:
        armed = await current_armed()
    except (RuntimeError, asyncio.TimeoutError):
        return "Rejected takeoff command. Armed status is not available."

    if not armed:
        return "Rejected takeoff command. Drone is not armed."

    await drone.action.takeoff()
    return "Takeoff command sent."


@tool
async def land():
    """Command the drone to land."""
    await drone.action.land()
    return "Land command sent."


@tool
async def hold():
    """Command the drone to hold current position."""
    await drone.action.hold()
    return "Hold command sent."


@tool
async def return_to_launch():
    """Command the drone to return to launch."""
    await drone.action.return_to_launch()
    return "Return-to-launch command sent."


@tool
async def set_current_speed(speed_m_s: float):
    """Set current mission/action speed in meters per second."""
    validation_error = validate_altitude_and_speed(speed_m_s=speed_m_s)
    if validation_error:
        return validation_error

    await drone.action.set_current_speed(speed_m_s)
    return f"Current speed set to {speed_m_s} m/s."


@tool
async def goto_location(latitude_deg: float, longitude_deg: float, absolute_altitude_m: float, yaw_deg: float = 0.0):
    """Command the drone to go to a global location."""
    validation_error = await validate_target_near_vehicle(latitude_deg, longitude_deg, MAX_GOTO_DISTANCE_M)
    if validation_error:
        return validation_error

    if absolute_altitude_m <= -100 or absolute_altitude_m > 10_000:
        return "Rejected unsafe absolute altitude."

    await drone.action.goto_location(latitude_deg, longitude_deg, absolute_altitude_m, yaw_deg)
    return "Goto location command sent."


@tool
async def upload_mission(mission_items: list[dict]):
    """Upload a mission from waypoint dictionaries."""
    items = []

    for item in mission_items:
        validation_error = await validate_target_near_vehicle(
            item["latitude_deg"],
            item["longitude_deg"],
            MAX_MISSION_DISTANCE_M,
        )
        if validation_error:
            return validation_error

        validation_error = validate_altitude_and_speed(
            relative_altitude_m=item["relative_altitude_m"],
            speed_m_s=item.get("speed_m_s", 5.0),
        )
        if validation_error:
            return validation_error

        items.append(
            mission_item(
                item["latitude_deg"],
                item["longitude_deg"],
                item["relative_altitude_m"],
                item.get("speed_m_s", 5.0),
                loiter_time_s=item.get("loiter_time_s", float("nan")),
                is_fly_through=item.get("is_fly_through", True),
                yaw_deg=item.get("yaw_deg", float("nan")),
            )
        )

    await drone.mission.upload_mission(MissionPlan(items))
    return f"Uploaded mission with {len(items)} item(s)."


@tool
async def start_mission():
    """Start the uploaded mission."""
    try:
        armed = await current_armed()
    except (RuntimeError, asyncio.TimeoutError):
        return "Rejected mission start. Armed status is not available."

    if not armed:
        return "Rejected mission start. Drone is not armed."

    await drone.mission.start_mission()
    return "Mission start command sent."


@tool
async def pause_mission():
    """Pause the current mission."""
    await drone.mission.pause_mission()
    return "Mission pause command sent."


@tool
async def clear_mission():
    """Clear the uploaded mission."""
    await drone.mission.clear_mission()
    return "Mission cleared."


@tool
async def set_current_mission_item(index: int):
    """Set the current mission item index."""
    await drone.mission.set_current_mission_item(index)
    return f"Current mission item set to {index}."


@tool
async def set_return_to_launch_after_mission(enable: bool):
    """Enable or disable return-to-launch after mission completion."""
    await drone.mission.set_return_to_launch_after_mission(enable)
    return f"Return-to-launch after mission set to {enable}."


@tool
async def mission_progress_status():
    """Check current mission progress."""
    try:
        progress = await read_one(drone.mission.mission_progress())
        return str(progress)
    except (RuntimeError, asyncio.TimeoutError):
        return "Mission progress is not available. Connect to PX4 first."


@tool
async def mission_finished_status():
    """Check whether the mission is finished."""
    try:
        finished = await drone.mission.is_mission_finished()
        return f"Mission finished: {finished}"
    except RuntimeError:
        return "Mission finished status is not available. Connect to PX4 first."


@tool
async def capture_incident_photo():
    """Capture a photo from the forward-facing gimbal camera at the incident observation point."""
    filename, error = _cam.capture("incident")
    if error:
        return f"Photo capture failed: {error}"
    return f"Photo captured and saved: {filename}. It is now visible in the web UI under Incident Camera."


@tool
async def build_incident_mission(
    incident_lat: float,
    incident_lon: float,
    home_lat: float,
    home_lon: float,
) -> dict:
    """
    Build the complete 6-waypoint incident mission.
    The drone flies to 5 m north of the incident at 2 m altitude facing the scene.
    Returns the waypoints list to pass directly to upload_mission — no manual math needed.
    """
    if home_lat == 0.0 and home_lon == 0.0:
        return {"error": "Home position is 0,0 — GPS not ready. Call home_position_status first and wait until a valid fix is returned before calling this tool."}
    obs_lat = round(incident_lat + 5.0 / 111_320, 7)
    obs_lon = round(incident_lon, 7)
    waypoints = [
        {"latitude_deg": home_lat,     "longitude_deg": home_lon,     "relative_altitude_m": 15, "speed_m_s": 3, "is_fly_through": True},
        {"latitude_deg": obs_lat,      "longitude_deg": obs_lon,      "relative_altitude_m": 15, "speed_m_s": 5, "is_fly_through": True},
        {"latitude_deg": obs_lat,      "longitude_deg": obs_lon,      "relative_altitude_m": 2,  "speed_m_s": 1, "is_fly_through": False, "loiter_time_s": 10, "yaw_deg": 180},
        {"latitude_deg": obs_lat,      "longitude_deg": obs_lon,      "relative_altitude_m": 15, "speed_m_s": 3, "is_fly_through": True},
        {"latitude_deg": home_lat,     "longitude_deg": home_lon,     "relative_altitude_m": 15, "speed_m_s": 5, "is_fly_through": True},
        {"latitude_deg": home_lat,     "longitude_deg": home_lon,     "relative_altitude_m": 5,  "speed_m_s": 2, "is_fly_through": True},
    ]
    return {"waypoints": waypoints}


Status_prompt = """You are the PX4 status agent.
You only check and report PX4 status.
Use status tools only.
Do not arm, connect, or perform actions.
If the user asks about the last mission, route, waypoints, or the area between points, use last_mission_status.
Never invent unavailable telemetry. If a status tool says unavailable, report unavailable."""


Action_prompt = """You are the PX4 action agent.
You only perform PX4 actions when the manager asks you.
Use action tools only.
For write/draw/shape requests, use create_shape_mission with the requested shape.
For mission creation, use create_area_mission when the user describes an area and style.
For denser coverage of the previous route/area, use create_denser_last_mission.
Use upload_mission only when exact waypoint dictionaries are already available.
Never call goto_location with 0,0 or with coordinates not provided by the user or produced by a mission-planning tool.
Never choose arbitrary global coordinates.
Do not use dangerous commands like kill, terminate, reboot, or shutdown."""


Manager_prompt = """You are the fully autonomous dispatch agent for Najm, Saudi Arabia's road incident response company.
You manage two sub-agents:
1- Status agent: checks connection, health, telemetry, arming, and mission status.
2- Action agent: connects, arms/disarms, takes off, lands, goes to locations, and controls missions.

YOUR CORE RULE: Never ask the user a question. Never wait for confirmation. Never ask for missing details — derive everything yourself from telemetry or use the fixed defaults below. The user gives you a task and you execute it completely, start to finish, on your own.

SAFETY RULES:
Never invent global coordinates. Only use coordinates from the user's message or current PX4 telemetry.
Never summarize a tool call as successful unless its result clearly confirms success.
Every waypoint-to-waypoint segment must either change ONLY altitude (same lat/lon) OR change ONLY lat/lon (same altitude). Never both at once.
Never use goto_location for multi-step sequences. Any mission with more than one leg must be a single upload_mission call with all waypoints listed in order.
If the user reports loss of control or runaway behavior, immediately ask the action agent to hold or land, then check status.
If the drone is not connected, connect first before doing anything else.
Respond in the same language the user uses (Arabic or English). Keep replies short — one paragraph maximum.

=== INCIDENT RESPONSE — PRIMARY MISSION (FULLY AUTOMATIC) ===
Trigger: the user's message contains a latitude and longitude, whether phrased as bare numbers ("47.39, 8.54"), or combined with any description such as "car accident", "road accident", "vehicle crash", "incident", "حادث سيارة", "حادث", or any similar phrase. The coordinates are the only required piece of information — all other words are context, not commands.

Do NOT ask the user anything. Execute this full sequence autonomously:

STEP 1 — Connect if needed:
  Ask status agent for connection_status. If not connected, ask action agent to connect, then verify connection.

STEP 2 — Get home position:
  Ask status agent for home_position_status. Extract home_lat and home_lon from the result.
  If the result says 0,0 or not ready, wait 3 seconds and call home_position_status again. Repeat until a valid non-zero fix is returned. Never proceed with 0,0.

STEP 3 — Clear old mission:
  Ask action agent to clear_mission.

STEP 4 — Upload the mission:
  Call the action agent with build_incident_mission(incident_lat, incident_lon, home_lat, home_lon).
  That tool returns {"waypoints": [...]} — pass those waypoints DIRECTLY to upload_mission without changing any values.
  Do NOT compute or modify any coordinates yourself.

STEP 5 — Arm:
  Ask status agent for arming_status. If not armed, ask action agent to arm. Verify with status agent.

STEP 6 — Take off:
  Ask status agent for in_air_status. If not in air, ask action agent to takeoff.

STEP 7 — Start mission:
  Ask action agent to start_mission.

STEP 8 — Monitor and capture photo:
  Ask status agent to monitor mission_progress until waypoint index 2 is reached, then ask action agent to capture_incident_photo. Continue monitoring until mission_finished_status confirms the mission is complete.

STEP 9 — Land:
  Only after mission_finished_status confirms completion, ask action agent to land.

Tell the user the mission is underway after STEP 7, then report the photo and final landing when done.

=== SHAPE/LETTER MISSIONS ===
When the user asks to draw/write a shape from current position, do not ask for coordinates.
Sequence: connect if needed → position_status → create_shape_mission → arm if needed → takeoff if not in air → start_mission → monitor.

=== AREA EXPLORATION MISSIONS ===
Get center coordinates from position_status automatically. Use safe defaults for all parameters unless the user specifies them.
If the user asks to re-scan the previous area more densely, use create_denser_last_mission."""











status_agent = Agent(
    name="px4_status_agent",
    model=model,
    tools=[
        connection_status,
        health_status,
        position_status,
        home_position_status,
        battery_status,
        gps_status,
        flight_mode_status,
        in_air_status,
        arming_status,
        attitude_status,
        landed_state_status,
        full_status,
        monitor_status,
        mission_progress_status,
        mission_finished_status,
    ],
    system_prompt=Status_prompt,
)


action_agent = Agent(
    name="px4_action_agent",
    model=model,
    tools=[
        connect,
        Arming,
        disarm,
        set_takeoff_altitude,
        takeoff,
        land,
        hold,
        return_to_launch,
        set_current_speed,
        goto_location,
        upload_mission,
        start_mission,
        pause_mission,
        clear_mission,
        set_current_mission_item,
        set_return_to_launch_after_mission,
        capture_incident_photo,
        build_incident_mission,
    ],
    system_prompt=Action_prompt,
)


manager_agent = Agent(
    model=model,
    tools=[status_agent, action_agent],
    system_prompt=Manager_prompt,
)

async def ask_manager(message):
    response = await manager_agent.invoke_async(message)
    return str(response)


async def main():
    while True:
        message = str(input("\nUser: \n"))

        print("AI: ")
        response = await ask_manager(message)
        print(response)

        if message == "exit":
            break


if __name__ == "__main__":
    asyncio.run(main())





