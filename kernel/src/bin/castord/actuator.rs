//! Node-local actuator identity configuration for attempt-scoped payload delivery.

use serde::Deserialize;
use std::fs;
use std::io;
use std::os::unix::net::UnixStream;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ActuatorTrust {
    pub peer_uid: u32,
    pub actuator_id: String,
}

impl ActuatorTrust {
    pub fn load() -> io::Result<Option<Self>> {
        let Some(path) = std::env::var_os("CASTORD_ACTUATOR_TRUST_CONFIG") else {
            return Ok(None);
        };
        let config: Self = serde_json::from_slice(&fs::read(path)?).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidData, "invalid actuator trust config")
        })?;
        if config.actuator_id.trim().is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid actuator trust configuration",
            ));
        }
        Ok(Some(config))
    }

    pub fn matches_peer(&self, stream: &UnixStream) -> bool {
        super::evidence::peer_uid(stream).ok() == Some(self.peer_uid)
    }
}
