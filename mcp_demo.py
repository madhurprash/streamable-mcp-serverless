import streamlit as st
import subprocess, threading, queue, time, os, requests, json
from pathlib import Path
import pandas as pd

# --- Page config & CSS ---
st.set_page_config(page_title="MCP Terminal", layout="wide")
st.markdown("""
<style>
  .terminal {
    background: #212529; color: #f8f9fa; font-family: monospace; padding: 1rem;
    border-radius: .25rem; white-space: pre-wrap; overflow-y: auto;
    height: 300px; margin-bottom: 1rem;
  }
  .stButton button { background: #4CAF50; color: white; border:none; border-radius:4px; }
</style>
""", unsafe_allow_html=True)

# --- Session state defaults ---
if "process" not in st.session_state:
    st.session_state.process = None
if "running" not in st.session_state:
    st.session_state.running = False
if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "server_url" not in st.session_state:
    st.session_state.server_url = "https://5aexcfie69.execute-api.us-east-1.amazonaws.com/prod/mcp"
if "output_q" not in st.session_state:
    st.session_state.output_q = queue.Queue()
if "cmd_q" not in st.session_state:
    st.session_state.cmd_q = queue.Queue()
if "stop_evt" not in st.session_state:
    st.session_state.stop_evt = threading.Event()
if "terminal_lines" not in st.session_state:
    st.session_state.terminal_lines = []
if "tools" not in st.session_state:
    st.session_state.tools = []

# --- Helpers ---
def drain_output():
    """Move all lines from the queue into our session list."""
    q = st.session_state.output_q
    while not q.empty():
        st.session_state.terminal_lines.append(q.get_nowait())

def read_output(proc, stop_evt, out_q):
    """Read child stdout and enqueue lines."""
    while not stop_evt.is_set():
        line = proc.stdout.readline()
        if line:
            out_q.put(line.rstrip())
        elif proc.poll() is not None:
            out_q.put(f"[Client exited code={proc.returncode}]")
            break
        else:
            time.sleep(0.1)

def write_input(proc, cmd_q, stop_evt):
    """Dequeue commands and write to child stdin."""
    while not stop_evt.is_set():
        try:
            cmd = cmd_q.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        except:
            break

def http_initialize(url):
    """Issue JSON-RPC initialize over HTTP to get session_id."""
    payload = {
      "jsonrpc":"2.0","method":"initialize",
      "params":{
        "clientInfo":{"name":"streamlit-client","version":"1.0"},
        "protocolVersion":"2025-03-26","capabilities":{}
      },
      "id": f"init-{int(time.time())}"
    }
    try:
        r = requests.post(
            url, json=payload,
            headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"},
            timeout=5
        )
        return r.headers.get("Mcp-Session-Id")
    except:
        return None

def start_client():
    """Install deps, spawn client, initialize session, and start I/O threads."""
    if st.session_state.running:
        return

    root = Path(__file__).parent.resolve()
    env = os.environ.copy()
    env["MCP_SERVER_URL"] = st.session_state.server_url

    # 1) npm install
    st.session_state.terminal_lines.append("📦 npm install…")
    completed = subprocess.run(
        ["npm","install"], cwd=root,
        capture_output=True, text=True
    )
    for ln in completed.stdout.splitlines():
        st.session_state.terminal_lines.append(ln)
    for ln in completed.stderr.splitlines():
        st.session_state.terminal_lines.append(ln)

    # 2) spawn TSX client
    st.session_state.terminal_lines.append("🚀 Spawning MCP client…")
    proc = subprocess.Popen(
        ["npx","tsx","src/client_streamlit.ts"],
        cwd=root, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    st.session_state.process = proc
    st.session_state.running = True
    st.session_state.stop_evt.clear()

    # 3) HTTP initialize
    sid = http_initialize(st.session_state.server_url)
    if sid:
        st.session_state.session_id = sid
        st.session_state.terminal_lines.append(f"✔️ Session initialized: {sid}")
    else:
        st.session_state.terminal_lines.append("❌ Failed HTTP initialize")

    # 4) start I/O threads
    threading.Thread(
        target=read_output,
        args=(proc, st.session_state.stop_evt, st.session_state.output_q),
        daemon=True
    ).start()
    threading.Thread(
        target=write_input,
        args=(proc, st.session_state.cmd_q, st.session_state.stop_evt),
        daemon=True
    ).start()

def stop_client():
    """Stop subprocess and threads."""
    if not st.session_state.running:
        return
    st.session_state.stop_evt.set()
    try:
        st.session_state.process.terminate()
        st.session_state.process.wait(2)
    except:
        st.session_state.process.kill()
    st.session_state.running = False
    st.session_state.process = None
    st.session_state.terminal_lines.append("🛑 Client stopped")

def send_cmd(cmd: str):
    """Enqueue a command for the client."""
    if st.session_state.running:
        st.session_state.terminal_lines.append(f"> {cmd}")
        st.session_state.cmd_q.put(cmd)
    else:
        st.session_state.terminal_lines.append("⚠️ Client not running")

def fetch_tools():
    """Fetch available tools via HTTP and show in a table."""
    headers={
      "Content-Type":"application/json",
      "Accept":"application/json",
      "Mcp-Session-Id": st.session_state.session_id
    }
    payload={"jsonrpc":"2.0","method":"tools/list","params":{},"id":f"list-{int(time.time())}"}
    r = requests.post(st.session_state.server_url, headers=headers, json=payload, timeout=5)
    if r.ok:
        st.session_state.tools = r.json().get("result",{}).get("tools",[])
    else:
        st.session_state.terminal_lines.append(f"❌ list-tools HTTP {r.status_code}")

# --- UI ---
st.title("MCP Terminal")

# Top bar: URL input + Start/Stop
c1, c2 = st.columns([3,1])
with c1:
    st.session_state.server_url = st.text_input("MCP_SERVER_URL", st.session_state.server_url)
with c2:
    if st.session_state.running:
        if st.button("Stop Client"):
            stop_client()
            st.experimental_rerun()
    else:
        if st.button("Start Client"):
            st.session_state.terminal_lines.clear()
            start_client()
            st.experimental_rerun()

# If running, show List Tools
if st.session_state.running:
    st.markdown("**Click** [List Tools] **to fetch available tools**")
    if st.button("List Tools"):
        fetch_tools()
    if st.session_state.tools:
        df = pd.DataFrame(st.session_state.tools)
        st.subheader("🔧 Available Tools")
        st.table(df[["name","description"]].rename(columns={"name":"Tool","description":"Description"}))

# Drain live output
drain_output()

# Terminal pane
st.markdown(
    "<div class='terminal'>" + "\n".join(st.session_state.terminal_lines) + "</div>",
    unsafe_allow_html=True
)

# Command input
cmd = st.text_input("Enter command:", key="cmd_inp", placeholder="e.g. call-tool greet '{\"name\":\"you\"}'")
if st.button("Send"):
    send_cmd(cmd)
    st.experimental_rerun()
