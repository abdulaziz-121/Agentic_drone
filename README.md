# Agentic Drone — Autonomous Incident Response System

An autonomous drone system that responds to a road-accident report written as a single sentence. The operator types the location; the drone takes off, flies to the scene, descends and frames the accident, captures a photo, returns home, and lands. Nobody pilots anything.

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/d58d98a8-9dca-43c3-8a45-db4d20a7680d" />

```
"car accident at 47.3977170, 8.5461293"
```

That one message is the entire input. Arabic or English.

---

## The problem

When a crash is reported, dispatch is blind. How many vehicles? How severe? Is the road blocked? Those facts decide how many ambulances to send, whether a tow truck is needed, and whether traffic has to be diverted — and they stay unknown until the first unit physically arrives.

A drone closes that gap in minutes, but flying one needs a trained pilot and a planned flight: a person and a skill that are rarely available the moment the call comes in.

**This project makes the pilot an AI agent.**

---

## Architecture

```
Operator message
      ↓
Flask web UI  ──────────────┐
      ↓                     │
   AI agent                 │  dashboard
      ↓                     │
   MAVSDK                   │
      ↓                     │
    PX4  ──→  Gazebo        │
      │                     │
      └─ ros_gz_bridge ─→ ROS 2 nodes ─→ telemetry, camera, safety
```

Responsibilities are split by layer, and each layer can be tested on its own:

| Layer | Owns |
|---|---|
| PX4 + MAVSDK | Flight control |
| ROS 2 | Perception and data transport |
| AI agent | Decision-making |
| Flask + JS | Presentation |

---

## What's in it

### 1. The agent layer

An agent built with **Strands Agents** interprets the incident report, then calls Python tools wrapping MAVSDK: connect, read position and health, arm, build and upload a mission, start it, monitor progress, return to launch, land.

It constructs a six-waypoint mission — takeoff, transit, descend and capture, ascend, return, land — and accepts direct commands mid-flight (`climb 10 metres`, `fly left`, `point the camera down`) so the operator can change their mind while the drone is airborne.

The model is swappable through an environment variable rather than a code change.

### 2. The ROS 2 layer

ROS 2 is the backbone. Every piece of sensor data and every perception command travels through it. The Gazebo camera is bridged in via `ros_gz_bridge`, and each node owns one responsibility:

| Node | Responsibility |
|---|---|
| `camera_node` | Subscribes to the bridged image stream, exposes a photo-capture service |
| `telemetry_node` | Reads PX4, publishes `NavSatFix` position and `BatteryState` |
| `safety_node` | Watches battery, altitude ceiling, and distance from launch; raises alerts before a limit is crossed |
| `ground_station` | Terminal monitoring — the system is fully usable with no web app |
| `web_node` | Runs the dashboard as a ROS node |

**Custom interfaces** rather than forcing everything into standard messages: a `DroneTelemetry` message, an `AimGimbal` service, and a `RespondToIncident` action with a dedicated mission action server that reports progress and supports cancellation.

Photo capture is a **service, not a topic** — deliberately. Capturing is a one-shot request that needs a confirmed reply: success plus the resulting filename. Topics are one-way streams, which is right for GPS and battery and wrong for commands.

The whole system starts with **one `ros2 launch` command** that brings up the bridge, every node, and the dashboard together.

### 3. The dashboard

Flask server, vanilla JavaScript frontend, Leaflet map. Shows the live camera feed, position and flight path, altitude, battery, heading, mission progress as a timeline, safety alerts, and the incident photo when it arrives. Agent responses stream to the browser token by token.

The web app sits **outside** ROS 2 as a service client only. The nodes own perception; the web app just consumes it. The drone system works end to end from the terminal with the dashboard switched off.

### 4. The simulation

A custom Gazebo world containing two separate accident scenes, with a gimbal-equipped quadcopter configured to fly in it.

---

## Engineering decisions

**The agent decides, the code calculates.**
An early version asked the agent to compute waypoint positions. It flew 61 metres instead of 5. Moving every geometric calculation into deterministic Python functions eliminated a **56 metre targeting error**.

**Safety limits live in code, not in the prompt.**
A 50 m altitude ceiling, a 500 m mission radius, and a 10 m/s speed cap. Any tool call that violates a limit is rejected before it reaches the drone, with a message explaining why — so the agent cannot talk its way past a safety constraint.

**Coordinates are validated before they're acted on.**
Invalid latitude and longitude values, and the specific `0,0` case, are rejected outright.

---

## Problems solved

**The gimbal camera pointed at the sky.** Raised the joint limit in the model definition, then changed the mission so the drone flies to a position *beside* the incident at low altitude facing the scene, rather than tilting down over it. Better framing, and it removed a dependency on gimbal precision.

**PX4 reports a home position of `0,0` for the first few seconds after startup.** The tools detect that specific value and tell the agent to wait and retry, instead of planning a mission around a meaningless origin.

**Two processes needed to talk to PX4 at once** — the agent for offboard control, the telemetry node for reading state. A single UDP port can't be shared, so they were separated: `14540` for control, `14550` read-only for telemetry, with separate MAVSDK server ports.

**Simulator frame vs. real GPS.** Derived the mapping between the simulator's local frame and GPS coordinates from the world origin, so a typed latitude and longitude lands on the actual accident model in the scene.

---

## Stack

| | |
|---|---|
| Middleware | ROS 2 Humble (`rclpy`, `ros_gz_bridge`, `cv_bridge`) |
| Flight controller | PX4 (SITL) |
| Simulator | Gazebo Harmonic |
| Drone control | MAVSDK Python |
| Agent framework | Strands Agents |
| Vision | OpenCV |
| Dashboard | Flask, vanilla JavaScript, Leaflet |
| Environment | Python 3.10 on Ubuntu 22.04 |

---

## Status

Full autonomous mission execution, live map, camera feed, gimbal control, mission progress reporting, and safety monitoring all work in PX4 and Gazebo simulation, launched with a single command.

**Next:** flight on physical hardware.

---

## Author

**Abdulaziz Mazyad** — Electrical Engineer
[LinkedIn](https://linkedin.com/in/abdulaziz-mazyad) · amazyad01@gmail.com

Built during the Unmanned Systems Technologies Bootcamp at Tuwaiq Academy.
