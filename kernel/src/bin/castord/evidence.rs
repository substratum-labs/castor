//! Node-local receipt authentication. No provider policy or journal writer lives here.
use castor_kernel::c06_composition::D1GovernedTurnAuthority;
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::Value;
use sha2::Sha256;
use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;

#[derive(Deserialize)]
pub(super) struct EvidenceTrust {
    issuer: String,
    pub peer_uid: u32,
    adapter_id: String,
    receipt_algorithm: String,
    key_hex: String,
    #[serde(default)]
    pub canonical_scopes: BTreeMap<String, String>,
    #[serde(skip)]
    key: Vec<u8>,
}

fn decode_hex(value: &str) -> Result<Vec<u8>, ()> {
    if !value.len().is_multiple_of(2) || !value.is_ascii() {
        return Err(());
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let hi = (pair[0] as char).to_digit(16).ok_or(())?;
            let lo = (pair[1] as char).to_digit(16).ok_or(())?;
            Ok((hi * 16 + lo) as u8)
        })
        .collect()
}

impl EvidenceTrust {
    pub fn load() -> io::Result<Option<Self>> {
        let Some(path) = std::env::var_os("CASTORD_EVIDENCE_TRUST_CONFIG") else {
            return Ok(None);
        };
        let mut config: Self = serde_json::from_slice(&fs::read(path)?).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidData, "invalid evidence trust config")
        })?;
        config.key = decode_hex(&config.key_hex)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid evidence key"))?;
        if config.key.len() < 32
            || config.issuer.is_empty()
            || config.adapter_id.is_empty()
            || config.receipt_algorithm != "HMAC-SHA256"
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid evidence trust configuration",
            ));
        }
        config.key_hex.clear();
        Ok(Some(config))
    }

    /// The signed receipt is the immutable Region, not guest-selected envelope fields.
    /// Canonical encoding is compact JSON with sorted keys (UTF-8, no whitespace).
    pub fn verify(&self, authority: &D1GovernedTurnAuthority, payload: &Value) -> Result<(), ()> {
        let text = |name| payload.get(name).and_then(Value::as_str).ok_or(());
        if text("issuer")? != self.issuer
            || text("adapter_id")? != self.adapter_id
            || payload
                .get("settlement_schema_version")
                .and_then(Value::as_u64)
                != Some(1)
            || text("dispatch_identity")? != text("stable_operation_id")?
        {
            return Err(());
        }
        let bytes = authority
            .storage()
            .read_region(text("evidence_region_id")?)
            .ok_or(())?;
        let receipt: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
        let mut signed = receipt.as_object().ok_or(())?.clone();
        // Reject envelope/receipt substitution, including the outcome and physical state.
        for (name, value) in &signed {
            if payload.get(name) != Some(value) {
                return Err(());
            }
        }
        let signature = signed.remove("signature").ok_or(())?;
        let signature = decode_hex(signature.as_str().ok_or(())?)?;
        let fields = [
            "attempt_id",
            "stable_operation_id",
            "request_digest",
            "issuer",
            "adapter_id",
            "settlement_schema_version",
            "resolution",
            "actuator_state",
        ];
        if signed.len() != fields.len() || fields.iter().any(|field| !signed.contains_key(*field)) {
            return Err(());
        }
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.key).map_err(|_| ())?;
        mac.update(&serde_json::to_vec(&signed).map_err(|_| ())?);
        mac.verify_slice(&signature).map_err(|_| ())?;
        match (
            text("resolution")?,
            text("actuator_state")?,
            text("proof_class")?,
        ) {
            ("Confirmed", "Committed", "ProviderConfirmation")
            | ("NotApplied", "TerminatedRejected", "VerifiableNonExecution") => (),
            _ => return Err(()),
        }
        let attempt_id = payload
            .get("attempt_id")
            .and_then(Value::as_u64)
            .ok_or(())?;
        if !authority.settlement_binding_matches(
            attempt_id,
            text("stable_operation_id")?,
            text("request_digest")?,
        ) {
            return Err(());
        }
        Ok(())
    }
}

#[cfg(any(
    target_os = "macos",
    target_os = "ios",
    target_os = "freebsd",
    target_os = "openbsd",
    target_os = "netbsd",
    target_os = "dragonfly"
))]
pub(super) fn peer_uid(stream: &UnixStream) -> io::Result<u32> {
    let mut uid = 0;
    let mut gid = 0;
    // SAFETY: live connected descriptor and valid writable uid/gid pointers.
    if unsafe { libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(uid)
}

#[cfg(any(target_os = "linux", target_os = "android"))]
pub(super) fn peer_uid(stream: &UnixStream) -> io::Result<u32> {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut size = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: buffer and length match ucred; stream owns a live descriptor.
    if unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut _ as *mut libc::c_void,
            &mut size,
        )
    } == -1
    {
        return Err(io::Error::last_os_error());
    }
    if size as usize != std::mem::size_of::<libc::ucred>() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid peer credentials",
        ));
    }
    Ok(cred.uid)
}
