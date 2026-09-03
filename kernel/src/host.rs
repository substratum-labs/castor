//! Frozen AISA v0.1 wire types and synchronous client framing.
//!
//! This module deliberately contains no daemon, dispatcher, or authority
//! implementation.  Phase 2 uses it to compile the socket contract tests;
//! Phase 3 supplies the `castord` listener that consumes these envelopes.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;

/// The maximum JSON payload accepted by the v0.1 framing contract.
pub const MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SyscallRequest {
    pub request_id: String,
    pub op: String,
    pub payload: Value,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct GatewayError {
    pub code: String,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SyscallResponse {
    pub request_id: String,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub outcome: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<GatewayError>,
}

/// Writes and reads the RFC's four-byte big-endian JSON framing.
pub fn write_framed(mut writer: impl Write, payload: &[u8]) -> io::Result<()> {
    if payload.is_empty() || payload.len() > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "AISA frame length must be between 1 and 16 MiB",
        ));
    }
    writer.write_all(&(payload.len() as u32).to_be_bytes())?;
    writer.write_all(payload)
}

/// Reads a bounded frame without allocating for an oversized length.
pub fn read_framed(mut reader: impl Read) -> io::Result<Vec<u8>> {
    let mut header = [0_u8; 4];
    reader.read_exact(&mut header)?;
    let length = u32::from_be_bytes(header) as usize;
    if length == 0 || length > MAX_FRAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid AISA frame length",
        ));
    }
    let mut payload = vec![0_u8; length];
    reader.read_exact(&mut payload)?;
    Ok(payload)
}

pub struct GatewayClient {
    stream: UnixStream,
}

impl GatewayClient {
    pub fn connect(socket_path: impl AsRef<Path>) -> io::Result<Self> {
        UnixStream::connect(socket_path).map(|stream| Self { stream })
    }

    pub fn request(&mut self, request: &SyscallRequest) -> io::Result<SyscallResponse> {
        let payload = serde_json::to_vec(request).map_err(json_error)?;
        write_framed(&mut self.stream, &payload)?;
        let response = read_framed(&mut self.stream)?;
        serde_json::from_slice(&response).map_err(json_error)
    }
}

fn json_error(error: serde_json::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}
