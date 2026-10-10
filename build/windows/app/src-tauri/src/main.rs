#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
//! The Laintas terminal window.
//!
//! The window draws a terminal emulator and owns one host process. The host
//! runs the CLI on a pty inside the bundled runtime and speaks the frame
//! protocol documented in `build/windows/term_host.py`: one type byte, a
//! four-byte big-endian length, the payload.
mod job;

use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{channel, Sender};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use base64::Engine;
use tauri::{Emitter, Manager};

const MAX_PAYLOAD: usize = 1 << 20;
const INPUT_CHUNK: usize = 32 * 1024;
const CLOSE_GRACE: Duration = Duration::from_secs(5);

struct Session {
    child: Child,
    sender: Sender<Vec<u8>>,
    // Dropping the job ends every process the session started.
    _job: Option<job::Job>,
}

#[derive(Default)]
struct TermState(Mutex<Option<Session>>);

fn frame(kind: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(5 + payload.len());
    out.push(kind);
    out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    out.extend_from_slice(payload);
    out
}

impl TermState {
    fn send(&self, data: Vec<u8>) -> Result<(), String> {
        let locked = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let session = locked.as_ref().ok_or("Laintas is not running")?;
        session.sender.send(data).map_err(|_| "Laintas is stopping".to_string())
    }

    /// Ask the CLI to end, as closing a terminal does, then stop waiting.
    fn stop(&self) {
        let session = self.0.lock().unwrap_or_else(|e| e.into_inner()).take();
        if let Some(mut session) = session {
            let _ = session.sender.send(frame(b'X', &[]));
            let deadline = Instant::now() + CLOSE_GRACE;
            while Instant::now() < deadline {
                if let Ok(Some(_)) = session.child.try_wait() {
                    return;
                }
                thread::sleep(Duration::from_millis(50));
            }
            let _ = session.child.kill();
            let _ = session.child.wait();
        }
    }
}

fn install_root() -> Result<PathBuf, String> {
    if let Some(value) = std::env::var_os("LAINTAS_TERMINAL_ROOT") {
        return Ok(PathBuf::from(value));
    }
    let executable = std::env::current_exe().map_err(|e| e.to_string())?;
    Ok(executable.parent().ok_or("Missing application directory")?.to_path_buf())
}

fn host_command(rows: u16, cols: u16) -> Result<Command, String> {
    let root = install_root()?;
    let bash = root.join("runtime").join("usr").join("bin").join("bash.exe");
    let launcher = root.join("app").join("build").join("windows").join("launch.sh");
    if !bash.is_file() {
        return Err(format!("the bundled runtime is missing ({})", bash.display()));
    }
    if !launcher.is_file() {
        return Err(format!("the bundled CLI is missing ({})", launcher.display()));
    }
    let mut command = Command::new(&bash);
    command.arg(&launcher).arg(rows.to_string()).arg(cols.to_string());
    command.env("MSYSTEM", "MSYS").env("CHERE_INVOKING", "1").env("LAINTAS_TERMINAL", "window");
    if let Some(directory) = std::env::args_os().nth(1) {
        command.env("LAINTAS_START_DIR", directory);
    }
    command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: the host must not open a console of its own.
        command.creation_flags(0x0800_0000);
    }
    Ok(command)
}

fn emit_output(app: &tauri::AppHandle, bytes: &[u8]) {
    let _ = app.emit("term:output", base64::engine::general_purpose::STANDARD.encode(bytes));
}

#[tauri::command]
fn term_start(app: tauri::AppHandle, state: tauri::State<TermState>, rows: u16, cols: u16) -> Result<(), String> {
    let mut locked = state.0.lock().unwrap_or_else(|e| e.into_inner());
    if locked.is_some() {
        return Ok(());
    }
    let mut child = host_command(rows, cols)?.spawn().map_err(|e| e.to_string())?;
    let job = job::Job::attach(&child).ok();
    let mut input = child.stdin.take().ok_or("Missing host input")?;
    let mut output = child.stdout.take().ok_or("Missing host output")?;
    let mut errors = child.stderr.take().ok_or("Missing host diagnostics")?;

    let (sender, receiver) = channel::<Vec<u8>>();
    thread::spawn(move || {
        for data in receiver {
            if input.write_all(&data).and_then(|_| input.flush()).is_err() {
                break;
            }
        }
    });

    // The CLI's own stderr goes to its pty. Anything on this pipe is the
    // launcher or the host failing, which the user has to be able to read.
    let diagnostics = app.clone();
    thread::spawn(move || {
        let mut chunk = [0u8; 4096];
        while let Ok(size) = errors.read(&mut chunk) {
            if size == 0 {
                break;
            }
            let mut text = Vec::with_capacity(size + 16);
            for byte in &chunk[..size] {
                if *byte == b'\n' {
                    text.push(b'\r');
                }
                text.push(*byte);
            }
            emit_output(&diagnostics, &text);
        }
    });

    let emitter = app.clone();
    thread::spawn(move || {
        let mut buffer: Vec<u8> = Vec::new();
        let mut chunk = vec![0u8; 64 * 1024];
        let mut status: i32 = 1;
        'read: loop {
            let size = match output.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(size) => size,
            };
            buffer.extend_from_slice(&chunk[..size]);
            while buffer.len() >= 5 {
                let length = u32::from_be_bytes([buffer[1], buffer[2], buffer[3], buffer[4]]) as usize;
                if length > MAX_PAYLOAD {
                    break 'read;
                }
                if buffer.len() < 5 + length {
                    break;
                }
                let kind = buffer[0];
                let payload: Vec<u8> = buffer[5..5 + length].to_vec();
                buffer.drain(..5 + length);
                match kind {
                    b'O' => emit_output(&emitter, &payload),
                    b'E' if payload.len() == 4 => {
                        status = i32::from_be_bytes([payload[0], payload[1], payload[2], payload[3]]);
                    }
                    _ => {}
                }
            }
        }
        let finished = {
            let state = emitter.state::<TermState>();
            let mut locked = state.0.lock().unwrap_or_else(|e| e.into_inner());
            let session = locked.take();
            session
        };
        if let Some(mut session) = finished {
            let _ = session.child.wait();
        }
        let _ = emitter.emit("term:exit", status);
    });

    *locked = Some(Session { child, sender, _job: job });
    Ok(())
}

#[tauri::command]
fn term_input(state: tauri::State<TermState>, data: Vec<u8>) -> Result<(), String> {
    for part in data.chunks(INPUT_CHUNK) {
        state.send(frame(b'I', part))?;
    }
    Ok(())
}

#[tauri::command]
fn term_resize(state: tauri::State<TermState>, rows: u16, cols: u16) -> Result<(), String> {
    let mut payload = Vec::with_capacity(4);
    payload.extend_from_slice(&rows.to_be_bytes());
    payload.extend_from_slice(&cols.to_be_bytes());
    state.send(frame(b'R', &payload))
}

fn main() {
    let app = tauri::Builder::default()
        .manage(TermState::default())
        .invoke_handler(tauri::generate_handler![term_start, term_input, term_resize])
        .setup(|app| {
            tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
                .title("Laintas")
                .inner_size(1040.0, 680.0)
                .min_inner_size(480.0, 300.0)
                // The window only ever shows the bundled terminal page.
                .on_navigation(|url| {
                    url.scheme() == "tauri"
                        || ((url.scheme() == "http" || url.scheme() == "https")
                            && url.host_str() == Some("tauri.localhost"))
                })
                .build()?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Failed to start the Laintas terminal window");
    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            app.state::<TermState>().stop();
        }
    });
}
