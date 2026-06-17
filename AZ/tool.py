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
last_mission = None
MAX_GOTO_DISTANCE_M = float(os.getenv("PX4_MAX_GOTO_DISTANCE_M", "200"))
MAX_MISSION_DISTANCE_M = float(os.getenv("PX4_MAX_MISSION_DISTANCE_M", "500"))
MAX_AREA_SIDE_M = float(os.getenv("PX4_MAX_AREA_SIDE_M", "300"))
MAX_ALTITUDE_M = float(os.getenv("PX4_MAX_ALTITUDE_M", "50"))
MAX_SPEED_M_S = float(os.getenv("PX4_MAX_SPEED_M_S", "10"))
DEFAULT_MISSION_ALTITUDE_M = float(os.getenv("PX4_DEFAULT_MISSION_ALTITUDE_M", "5"))
DEFAULT_MISSION_SPEED_M_S = float(os.getenv("PX4_DEFAULT_MISSION_SPEED_M_S", "3"))
DEFAULT_SHAPE_WIDTH_M = float(os.getenv("PX4_DEFAULT_SHAPE_WIDTH_M", "20"))
DEFAULT_SHAPE_HEIGHT_M = float(os.getenv("PX4_DEFAULT_SHAPE_HEIGHT_M", "20"))


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

def offset_to_lat_lon(latitude_deg, longitude_deg, north_m, east_m):
    latitude = latitude_deg + north_m / 111_320
    longitude = longitude_deg + east_m / (111_320 * math.cos(math.radians(latitude_deg)))
    return latitude, longitude

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


def build_area_waypoints(style, center_latitude_deg, center_longitude_deg, area_width_m, area_height_m, spacing_m):
    style = style.lower().strip()
    spacing_m = max(1, spacing_m)
    half_width = area_width_m / 2
    half_height = area_height_m / 2
    offsets = []

    if style in ["grid", "network", "lawnmower", "survey", "mapping"]:
        row_count = max(2, int(area_height_m / spacing_m) + 1)
        for row in range(row_count):
            north = -half_height + row * spacing_m
            north = min(north, half_height)
            west_to_east = row % 2 == 0
            east_values = [-half_width, half_width] if west_to_east else [half_width, -half_width]
            for east in east_values:
                offsets.append((north, east))

    elif style in ["perimeter", "boundary", "box"]:
        offsets = [
            (-half_height, -half_width),
            (-half_height, half_width),
            (half_height, half_width),
            (half_height, -half_width),
            (-half_height, -half_width),
        ]

    elif style in ["cross", "plus"]:
        offsets = [
            (0, -half_width),
            (0, half_width),
            (0, 0),
            (-half_height, 0),
            (half_height, 0),
        ]

    elif style in ["spiral", "square_spiral"]:
        left = -half_width
        right = half_width
        bottom = -half_height
        top = half_height

        while left <= right and bottom <= top:
            offsets.extend([(bottom, left), (bottom, right), (top, right), (top, left)])
            left += spacing_m
            right -= spacing_m
            bottom += spacing_m
            top -= spacing_m

    elif style in ["circle", "round"]:
        radius = min(half_width, half_height)
        point_count = max(8, int((2 * math.pi * radius) / spacing_m))
        for index in range(point_count + 1):
            angle = 2 * math.pi * index / point_count
            north = radius * math.cos(angle)
            east = radius * math.sin(angle)
            offsets.append((north, east))

    else:
        raise ValueError("Unsupported mission style. Use grid, network, lawnmower, perimeter, spiral, cross, or circle.")

    return [
        offset_to_lat_lon(center_latitude_deg, center_longitude_deg, north, east)
        for north, east in offsets
    ]


def path_length_m(positions):
    total = 0

    for index in range(1, len(positions)):
        total += distance_m(
            positions[index - 1][0],
            positions[index - 1][1],
            positions[index][0],
            positions[index][1],
        )

    return total


def mission_bounds(positions):
    latitudes = [position[0] for position in positions]
    longitudes = [position[1] for position in positions]
    return {
        "min_latitude_deg": min(latitudes),
        "max_latitude_deg": max(latitudes),
        "min_longitude_deg": min(longitudes),
        "max_longitude_deg": max(longitudes),
    }


def mission_center(positions):
    bounds = mission_bounds(positions)
    return (
        (bounds["min_latitude_deg"] + bounds["max_latitude_deg"]) / 2,
        (bounds["min_longitude_deg"] + bounds["max_longitude_deg"]) / 2,
    )


def remember_mission(kind, positions, relative_altitude_m, speed_m_s, details):
    global last_mission

    last_mission = {
        "kind": kind,
        "waypoints": [
            {
                "index": index,
                "latitude_deg": latitude,
                "longitude_deg": longitude,
                "relative_altitude_m": relative_altitude_m,
                "speed_m_s": speed_m_s,
            }
            for index, (latitude, longitude) in enumerate(positions)
        ],
        "bounds": mission_bounds(positions),
        "center": mission_center(positions),
        "path_length_m": path_length_m(positions),
        "details": details,
    }


def last_mission_summary():
    if last_mission is None:
        return "No mission has been created or uploaded in this session."

    center_latitude, center_longitude = last_mission["center"]
    bounds = last_mission["bounds"]
    return (
        f"Last mission kind: {last_mission['kind']}\n"
        f"Waypoint count: {len(last_mission['waypoints'])}\n"
        f"Path length: {last_mission['path_length_m']:.1f} m\n"
        f"Center: latitude {center_latitude:.7f}, longitude {center_longitude:.7f}\n"
        f"Area/bounds between waypoints: "
        f"lat {bounds['min_latitude_deg']:.7f} to {bounds['max_latitude_deg']:.7f}, "
        f"lon {bounds['min_longitude_deg']:.7f} to {bounds['max_longitude_deg']:.7f}\n"
        f"Details: {last_mission['details']}"
    )


def build_shape_offsets(shape, width_m, height_m):
    shape = shape.lower().strip()
    half_width = width_m / 2
    half_height = height_m / 2

    if shape in ["triangle"]:
        return [
            (0, 0),
            (height_m, half_width),
            (0, width_m),
            (0, 0),
        ]

    if shape in ["square", "box", "rectangle"]:
        return [
            (0, 0),
            (0, width_m),
            (height_m, width_m),
            (height_m, 0),
            (0, 0),
        ]

    if shape in ["cross", "plus", "+"]:
        return [
            (half_height, 0),
            (half_height, width_m),
            (half_height, half_width),
            (0, half_width),
            (height_m, half_width),
        ]

    if shape in ["circle", "round"]:
        point_count = 16
        radius = min(half_width, half_height)
        return [
            (
                half_height + radius * math.cos(2 * math.pi * index / point_count),
                half_width + radius * math.sin(2 * math.pi * index / point_count),
            )
            for index in range(point_count + 1)
        ]

    if shape in ["zigzag", "snake"]:
        return [
            (0, 0),
            (height_m, width_m / 3),
            (0, 2 * width_m / 3),
            (height_m, width_m),
        ]

    return [
        (0, 0),
        (height_m, width_m / 3),
        (0, 2 * width_m / 3),
        (height_m, width_m),
    ]


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
    waypoint_positions = []
    last_altitude_m = DEFAULT_MISSION_ALTITUDE_M
    last_speed_m_s = DEFAULT_MISSION_SPEED_M_S

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

        last_altitude_m = item["relative_altitude_m"]
        last_speed_m_s = item.get("speed_m_s", 5.0)
        waypoint_positions.append((item["latitude_deg"], item["longitude_deg"]))
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
    remember_mission(
        "uploaded",
        waypoint_positions,
        last_altitude_m,
        last_speed_m_s,
        "Mission uploaded from explicit waypoint dictionaries.",
    )
    return f"Uploaded mission with {len(items)} item(s)."


@tool
async def supported_mission_styles():
    """List mission styles that can be generated automatically."""
    return (
        "Supported area styles: grid, network, lawnmower, survey, mapping, perimeter, boundary, box, spiral, square_spiral, cross, plus, circle, round. "
        "Supported shape styles: triangle, square, rectangle, cross, plus, circle, zigzag. "
        "Unsupported shape names fall back to a safe zigzag path."
    )


@tool
async def last_mission_status():
    """Report the last mission created or uploaded in this session, including the area between waypoints."""
    return last_mission_summary()


@tool
async def create_area_mission(
    style: str,
    center_latitude_deg: float,
    center_longitude_deg: float,
    area_width_m: float,
    area_height_m: float,
    relative_altitude_m: float,
    spacing_m: float = 20.0,
    speed_m_s: float = 5.0,
    return_to_launch_when_finished: bool = True,
):
    """Create and upload an area exploration mission from a style and area settings."""
    validation_error = await validate_target_near_vehicle(
        center_latitude_deg,
        center_longitude_deg,
        MAX_MISSION_DISTANCE_M,
    )
    if validation_error:
        return validation_error

    if area_width_m <= 0 or area_height_m <= 0:
        return "Area width and height must be greater than 0."

    if area_width_m > MAX_AREA_SIDE_M or area_height_m > MAX_AREA_SIDE_M:
        return f"Rejected unsafe area size. Width and height must be at most {MAX_AREA_SIDE_M} m."

    validation_error = validate_altitude_and_speed(
        relative_altitude_m=relative_altitude_m,
        speed_m_s=speed_m_s,
    )
    if validation_error:
        return validation_error

    if spacing_m <= 0:
        return "Spacing must be greater than 0."

    try:
        waypoint_positions = build_area_waypoints(
            style,
            center_latitude_deg,
            center_longitude_deg,
            area_width_m,
            area_height_m,
            spacing_m,
        )
    except ValueError as error:
        return str(error)

    items = [
        mission_item(latitude, longitude, relative_altitude_m, speed_m_s)
        for latitude, longitude in waypoint_positions
    ]

    await drone.mission.set_return_to_launch_after_mission(return_to_launch_when_finished)
    await drone.mission.upload_mission(MissionPlan(items))
    remember_mission(
        style,
        waypoint_positions,
        relative_altitude_m,
        speed_m_s,
        (
            f"Area mission. Center=({center_latitude_deg}, {center_longitude_deg}), "
            f"area={area_width_m}x{area_height_m} m, spacing={spacing_m} m, "
            f"RTL after mission={return_to_launch_when_finished}."
        ),
    )

    return (
        f"Created and uploaded a {style} mission with {len(items)} waypoint(s). "
        f"Center=({center_latitude_deg}, {center_longitude_deg}), "
        f"area={area_width_m}x{area_height_m} m, altitude={relative_altitude_m} m, "
        f"spacing={spacing_m} m, speed={speed_m_s} m/s, "
        f"RTL after mission={return_to_launch_when_finished}."
    )


@tool
async def create_denser_last_mission(
    spacing_m: float = 10.0,
    relative_altitude_m: float | None = None,
    speed_m_s: float | None = None,
    return_to_launch_when_finished: bool = True,
):
    """Create a denser grid mission over the area covered by the last remembered mission."""
    if last_mission is None:
        return "No previous mission is remembered in this session."

    if spacing_m <= 0:
        return "Spacing must be greater than 0."

    bounds = last_mission["bounds"]
    center_latitude_deg, center_longitude_deg = last_mission["center"]
    area_height_m = distance_m(
        bounds["min_latitude_deg"],
        center_longitude_deg,
        bounds["max_latitude_deg"],
        center_longitude_deg,
    )
    area_width_m = distance_m(
        center_latitude_deg,
        bounds["min_longitude_deg"],
        center_latitude_deg,
        bounds["max_longitude_deg"],
    )
    remembered_waypoint = last_mission["waypoints"][0]
    altitude = relative_altitude_m or remembered_waypoint["relative_altitude_m"]
    speed = speed_m_s or remembered_waypoint["speed_m_s"]

    return await create_area_mission(
        "grid",
        center_latitude_deg,
        center_longitude_deg,
        max(area_width_m, spacing_m),
        max(area_height_m, spacing_m),
        altitude,
        spacing_m,
        speed,
        return_to_launch_when_finished,
    )


@tool
async def create_shape_mission(
    shape: str,
    duration_seconds: int = 60,
    relative_altitude_m: float = DEFAULT_MISSION_ALTITUDE_M,
    width_m: float = DEFAULT_SHAPE_WIDTH_M,
    height_m: float = DEFAULT_SHAPE_HEIGHT_M,
    return_to_launch_when_finished: bool = True,
):
    """Create and upload a safe shape mission starting from the current/home position."""
    if duration_seconds <= 0:
        return "Duration must be greater than 0 seconds."

    if width_m <= 0 or height_m <= 0:
        return "Shape width and height must be greater than 0."

    if width_m > MAX_AREA_SIDE_M or height_m > MAX_AREA_SIDE_M:
        return f"Rejected unsafe shape size. Width and height must be at most {MAX_AREA_SIDE_M} m."

    validation_error = validate_altitude_and_speed(relative_altitude_m=relative_altitude_m)
    if validation_error:
        return validation_error

    try:
        start = await current_position()
    except (RuntimeError, asyncio.TimeoutError):
        return "Cannot create shape mission because current position is not available. Connect PX4 first."

    start_latitude_deg = start.latitude_deg
    start_longitude_deg = start.longitude_deg

    if not valid_coordinate(start_latitude_deg, start_longitude_deg):
        return "Cannot create shape mission because current/home position is invalid."

    offsets = build_shape_offsets(shape, width_m, height_m)
    waypoint_positions = [
        offset_to_lat_lon(start_latitude_deg, start_longitude_deg, north, east)
        for north, east in offsets
    ]
    length_m = path_length_m(waypoint_positions)
    speed_m_s = min(MAX_SPEED_M_S, max(1.0, length_m / duration_seconds))

    validation_error = validate_altitude_and_speed(
        relative_altitude_m=relative_altitude_m,
        speed_m_s=speed_m_s,
    )
    if validation_error:
        return validation_error

    items = [
        mission_item(latitude, longitude, relative_altitude_m, speed_m_s)
        for latitude, longitude in waypoint_positions
    ]

    await drone.mission.set_return_to_launch_after_mission(return_to_launch_when_finished)
    await drone.mission.upload_mission(MissionPlan(items))
    remember_mission(
        f"shape:{shape}",
        waypoint_positions,
        relative_altitude_m,
        speed_m_s,
        (
            f"Shape mission. Shape={shape}, duration={duration_seconds} s, "
            f"width={width_m} m, height={height_m} m, RTL after mission={return_to_launch_when_finished}."
        ),
    )

    return (
        f"Created and uploaded a {shape} shape mission from current/home position with {len(items)} waypoint(s). "
        f"Duration target={duration_seconds} s, width={width_m} m, height={height_m} m, "
        f"altitude={relative_altitude_m} m, computed speed={speed_m_s:.2f} m/s, "
        f"RTL after mission={return_to_launch_when_finished}."
    )


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
async def observation_point(incident_lat: float, incident_lon: float) -> dict:
    """
    Compute the drone observation position 20 m north of the incident.
    Returns obs_lat, obs_lon to use in the mission waypoints.
    Always call this tool before building the incident mission — never compute the offset manually.
    """
    obs_lat = round(incident_lat + 20.0 / 111_320, 7)
    obs_lon = round(incident_lon, 7)
    return {"obs_lat": obs_lat, "obs_lon": obs_lon}


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
  Ask status agent for position_status. Extract home_lat and home_lon from the result. Never use 0,0.

STEP 3 — Clear old mission:
  Ask action agent to clear_mission.

STEP 4 — Upload the mission:
  First call the action agent with observation_point(incident_lat, incident_lon) to get obs_lat and obs_lon. Use ONLY those returned values — never compute the offset yourself.
  Then build EXACTLY ONE upload_mission call with these 6 waypoints in order (relative_altitude_m only, never absolute):
  1. {"latitude_deg": home_lat,  "longitude_deg": home_lon,  "relative_altitude_m": 15, "speed_m_s": 3, "is_fly_through": true}
  2. {"latitude_deg": obs_lat,   "longitude_deg": obs_lon,   "relative_altitude_m": 15, "speed_m_s": 5, "is_fly_through": true}
  3. {"latitude_deg": obs_lat,   "longitude_deg": obs_lon,   "relative_altitude_m": 2,  "speed_m_s": 1, "is_fly_through": false, "loiter_time_s": 10, "yaw_deg": 180}
  4. {"latitude_deg": obs_lat,   "longitude_deg": obs_lon,   "relative_altitude_m": 15, "speed_m_s": 3, "is_fly_through": true}
  5. {"latitude_deg": home_lat,  "longitude_deg": home_lon,  "relative_altitude_m": 15, "speed_m_s": 5, "is_fly_through": true}
  6. {"latitude_deg": home_lat,  "longitude_deg": home_lon,  "relative_altitude_m": 5,  "speed_m_s": 2, "is_fly_through": true}
  Waypoint 3 positions the drone 20 m north of the incident at 2 m altitude facing south (yaw 180°) so the forward camera captures the scene.

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
        battery_status,
        gps_status,
        flight_mode_status,
        in_air_status,
        arming_status,
        attitude_status,
        landed_state_status,
        full_status,
        monitor_status,
        last_mission_status,
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
        supported_mission_styles,
        create_denser_last_mission,
        create_area_mission,
        create_shape_mission,
        start_mission,
        pause_mission,
        clear_mission,
        set_current_mission_item,
        set_return_to_launch_after_mission,
        capture_incident_photo,
        observation_point,
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





