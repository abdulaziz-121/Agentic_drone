# Agentic Drone — Autonomous Incident Response System

A fully autonomous drone system that responds to incident reports (like car accidents) by navigating to the scene, capturing a photo, and displaying it on a web dashboard — all triggered by a single text message.

---

## Project Idea

The idea came from thinking about how emergency responders could get eyes on a scene before physically arriving. Instead of manually piloting a drone, you just type something like:

```
car accident at 47.3977128, 8.5461408
```

The AI agent takes it from there — it connects to the drone, gets the GPS home position, builds a full mission, arms the drone, flies it to 5 meters north of the incident at low altitude facing the scene, captures a photo, and returns home. Everything is automatic.

---

## What the System Does

1. User types an incident location in the web UI
2. The AI agent (built with Strands Agents) interprets the message
3. It builds a 6-waypoint mission:
   - Takeoff → Fly to scene → Descend to 2m → Hover 10 seconds (capture photo) → Ascend → Return home → Land
4. The drone executes the mission in Gazebo SITL
5. The camera image is streamed from Gazebo using `gz-transport` and saved
6. The photo appears live on the web dashboard

---

## The World

The simulation uses a custom world called `external_world_1`. It contains:

- A city-like environment with streets and buildings
- A car accident model placed at a specific location in the scene
- World GPS origin: lat=47.397971, lon=8.546163

The world was designed to simulate a real urban environment where a drone could be deployed for incident response.

---

## The Drone Model

The drone used is `x500_gimbal` — a quadcopter model provided by PX4 with a gimbal camera attached.

Customizations made to the gimbal model (`~/.simulation-gazebo/models/gimbal/model.sdf`):
- Added `<topic>/drone/camera/image</topic>` to the camera sensor to publish on a known topic
- Increased the camera pitch joint upper limit from 0.7854 to 1.5708 (to allow full downward tilt)
- Camera resolution set to 1280×720 at 30fps

The camera faces forward, so the drone is positioned north of the incident and yawed to face south — giving a clear view of the accident scene.

---

## Tech Stack

| Component | Tool |
|---|---|
| Flight controller | PX4 SITL |
| Simulator | Gazebo (Harmonic) |
| Drone control | MAVSDK Python |
| AI Agent | Strands Agents (Claude claude-sonnet-4-6) |
| Camera streaming | gz-transport13 (Gazebo native, no ROS2) |
| Image processing | OpenCV + NumPy |
| Web server | Flask |
| Frontend | HTML + CSS + vanilla JavaScript |

---

## Prerequisites

- Ubuntu 22.04 or 24.04
- PX4-Autopilot (built from source)
- Gazebo Harmonic
- Python 3.10+
- gz-transport13 Python bindings (`gz.transport13`, `gz.msgs10`)
- An Anthropic API key

---

## Installation

**1. Clone the repo**
```bash
git clone <your-repo-url>
cd Agentic_drone
```

**2. Install Python dependencies**
```bash
pip install -r AZ/req.txt
pip install mavsdk
```

**3. Set up your API key**

Create a file `AZ/.env`:
```
ANTHROPIC_API_KEY=your_key_here
```

---

## How to Run

You need 3 terminals open at the same time.

**Terminal 1 — Start PX4 SITL with Gazebo**
```bash
cd ~/PX4-Autopilot
PX4_GZ_WORLD=external_world_1 make px4_sitl gz_x500_gimbal
```

Wait until you see `[Ready for takeoff]` in the terminal.

**Terminal 2 — Start the web server**
```bash
cd ~/Desktop/agentic/Agentic_drone/AZ
python app.py
```

**Terminal 3 — Open the dashboard**

Open your browser and go to:
```
http://localhost:5000
```

---

## How to Use

Once everything is running, type in the chat box:

```
car accident at 47.3977128, 8.5461408
```

The agent will handle everything automatically. You can watch the drone fly in Gazebo and the photo will appear in the "Incident Camera" section of the dashboard when it arrives at the scene.

---

## Project Structure

```
Agentic_drone/
├── AZ/
│   ├── app.py          # Flask web server + API endpoints
│   ├── tool.py         # AI agent definition + all drone tools
│   ├── camera.py       # Gazebo camera subscriber (gz-transport)
│   ├── req.txt         # Python dependencies
│   ├── static/
│   │   ├── app.js      # Frontend logic
│   │   ├── style.css   # UI styling
│   │   └── photos/     # Captured incident photos
│   └── templates/
│       └── index.html  # Web dashboard
└── README.md
```

---

## Key Challenges

**1. Camera orientation**
The gimbal camera was initially looking upward instead of at the scene. Multiple attempts to tilt it programmatically failed because the joint angle limit was too small (45°). The fix was to raise the limit in the SDF file and change the approach: instead of pointing the camera down, the drone flies beside the accident and faces it — using the forward-facing camera naturally.

**2. GPS coordinate math**
Early versions had the AI agent compute the observation point coordinates inline, which led to large errors (drone flew 61m instead of 5m from the scene). The fix was to move all coordinate math into a Python tool (`build_incident_mission`) that returns the complete waypoint list — the agent just passes it through without touching the numbers.

**3. Home position timing**
The drone's home GPS position returned 0,0 during the first few seconds after startup. Using `drone.telemetry.home()` instead of `drone.telemetry.position()` made it more reliable, and the agent was instructed to retry if it gets 0,0.
