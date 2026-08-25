// One telemetry websocket, shared by every view via subscribe/publish.
//
// The old code had a single connect() that called render() directly, so
// every panel that wanted live data had to be reachable from that one
// function -- which is a large part of why everything ended up in one
// 960-line file with shared globals. A store inverts that: the socket
// doesn't know who is listening, and each tab subscribes and unsubscribes
// as it is shown or hidden.
//
// Subscribers get the full vessel list on every tick, plus the current
// connection state. Late subscribers immediately receive the last known
// snapshot rather than waiting up to half a second for the next tick.

const subscribers = new Set();
const connectionSubscribers = new Set();

let lastSnapshot = { krpc_connected: false, vessels: [] };
let connectionState = "connecting"; // connecting | open | waiting-for-ksp | closed
let socket = null;

function publish() {
  for (const fn of subscribers) {
    try {
      fn(lastSnapshot.vessels, lastSnapshot);
    } catch (e) {
      // One broken subscriber must not stop the others from updating.
      console.error("telemetry subscriber failed", e);
    }
  }
}

function setConnectionState(state) {
  if (state === connectionState) return;
  connectionState = state;
  for (const fn of connectionSubscribers) {
    try {
      fn(state);
    } catch (e) {
      console.error("connection subscriber failed", e);
    }
  }
}

export function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws/telemetry`);

  socket.onopen = () => setConnectionState("open");

  socket.onclose = () => {
    setConnectionState("closed");
    setTimeout(connect, 2000);
  };

  socket.onerror = () => socket.close();

  socket.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (e) {
      // Confirmed live: a NaN in one vessel's telemetry (see the backend
      // fix in telemetry.py) made the *entire* tick's JSON invalid, so
      // every craft's display silently stopped updating for that tick --
      // not just the affected vessel -- and repeated on every subsequent
      // tick for as long as the condition lasted, with the connection
      // indicator still showing "connected" throughout. The real fix is
      // server-side (never emit invalid JSON), but failing loud-but-safe
      // here means a future bad payload degrades to "one skipped tick"
      // instead of a wall of uncaught exceptions.
      console.error("telemetry: skipping malformed message", e);
      return;
    }
    setConnectionState(data.krpc_connected ? "open" : "waiting-for-ksp");
    lastSnapshot = data;
    publish();
  };
}

/** Subscribe to vessel updates. Returns an unsubscribe function. */
export function subscribe(fn) {
  subscribers.add(fn);
  // Hand over what we already have, so a tab shown between ticks renders
  // immediately instead of appearing empty for half a second.
  if (lastSnapshot.vessels.length || lastSnapshot.krpc_connected) {
    fn(lastSnapshot.vessels, lastSnapshot);
  }
  return () => subscribers.delete(fn);
}

/** Subscribe to connection-state changes. Returns an unsubscribe function. */
export function subscribeConnection(fn) {
  connectionSubscribers.add(fn);
  fn(connectionState);
  return () => connectionSubscribers.delete(fn);
}

export function getVessels() {
  return lastSnapshot.vessels;
}

export function getVessel(id) {
  return lastSnapshot.vessels.find((v) => v.id === id) || null;
}
