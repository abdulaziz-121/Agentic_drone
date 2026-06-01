const form = document.querySelector("#commandForm");
const input = document.querySelector("#messageInput");
const sendButton = document.querySelector("#sendButton");
const log = document.querySelector("#log");
const state = document.querySelector("#connectionState");
const clearLog = document.querySelector("#clearLog");
const quickButtons = document.querySelectorAll("[data-command]");

const modeValue = document.querySelector("#modeValue");
const armedValue = document.querySelector("#armedValue");
const missionValue = document.querySelector("#missionValue");
const updatedValue = document.querySelector("#updatedValue");

function setState(label, className) {
  state.className = `status-pill ${className || ""}`;
  state.lastChild.textContent = ` ${label}`;
}

function addLog(kind, title, body) {
  const entry = document.createElement("div");
  entry.className = `entry ${kind}`;
  const now = new Date().toLocaleTimeString();
  entry.innerHTML = `
    <div class="entry-meta">${title} • ${now}</div>
    <div class="entry-body"></div>
  `;
  entry.querySelector(".entry-body").textContent = body;
  log.prepend(entry);
}

function updateTelemetry(text, time) {
  updatedValue.textContent = time || new Date().toLocaleTimeString();

  const modeMatch = text.match(/flight_mode:\s*([^\n]+)/i) || text.match(/Flight mode:\s*([^\n]+)/i);
  const armedMatch = text.match(/armed:\s*([^\n]+)/i) || text.match(/Armed:\s*([^\n]+)/i);
  const missionMatch = text.match(/mission_progress:\s*([^\n]+)/i) || text.match(/Mission progress:\s*([^\n]+)/i);

  if (modeMatch) modeValue.textContent = modeMatch[1].trim().slice(0, 40);
  if (armedMatch) armedValue.textContent = armedMatch[1].trim().slice(0, 40);
  if (missionMatch) missionValue.textContent = missionMatch[1].trim().slice(0, 40);
}

async function sendCommand(message) {
  setState("Working", "busy");
  sendButton.disabled = true;
  quickButtons.forEach((button) => button.disabled = true);
  addLog("user", "User", message);

  try {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message}),
    });
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || "Request failed");
    }

    addLog("agent", "PX4 Manager", data.response);
    updateTelemetry(data.response, data.time);
    setState("Ready", "ok");
  } catch (error) {
    addLog("error", "Error", error.message);
    setState("Error", "error");
  } finally {
    sendButton.disabled = false;
    quickButtons.forEach((button) => button.disabled = false);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  sendCommand(message);
});

quickButtons.forEach((button) => {
  button.addEventListener("click", () => {
    sendCommand(button.dataset.command);
  });
});

clearLog.addEventListener("click", () => {
  log.innerHTML = "";
});

setState("Standby", "");
