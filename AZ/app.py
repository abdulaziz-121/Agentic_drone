import asyncio
import threading
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import camera as _cam
from tool import ask_manager


app = Flask(__name__)

agent_loop = asyncio.new_event_loop()


def start_agent_loop():
    asyncio.set_event_loop(agent_loop)
    agent_loop.run_forever()


threading.Thread(target=start_agent_loop, daemon=True).start()


async def run_agent(message):
    return await ask_manager(message)


def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, agent_loop)
    return future.result()


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/command")
def command():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()

    if not message:
        return jsonify({"error": "Message is required."}), 400

    try:
        response = run_async(run_agent(message))
    except Exception as error:
        return jsonify({"error": str(error)}), 500

    return jsonify(
        {
            "message": message,
            "response": response,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
    )


@app.get("/api/photo/latest")
def latest_photo():
    filename = _cam.latest_filename()
    if not filename:
        return jsonify({"photo": None})
    return jsonify({"photo": f"/static/photos/{filename}"})


@app.get("/api/status")
def status():
    try:
        response = run_async(run_agent("Give me a concise full PX4 status."))
    except Exception as error:
        return jsonify({"error": str(error)}), 500

    return jsonify(
        {
            "response": response,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
