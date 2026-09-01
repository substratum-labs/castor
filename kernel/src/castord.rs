//! Minimal composition boundary for the T-288-C single-node vertical slice.
//!
//! This type owns C-01 and C-04 mechanisms under one explicit state root. It
//! does not add an RPC interface or any new protocol authority.

use crate::c01_storage::D1DurableStorage;
use crate::c04_adapter::{D1EffectAdapter, EffectProvider};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

pub struct Castord<P: EffectProvider> {
    state_root: PathBuf,
    storage: D1DurableStorage,
    effect_adapter: D1EffectAdapter<P>,
}

impl<P: EffectProvider> Castord<P> {
    pub fn initialize(
        state_root: impl AsRef<Path>,
        adapter_id: &str,
        assurance_profile: &str,
        provider: P,
    ) -> io::Result<Self> {
        let state_root = state_root.as_ref().to_path_buf();
        fs::create_dir_all(&state_root)?;
        let core_root = state_root.join("core");
        let adapter_root = state_root.join("adapter");
        if core_root.exists() || adapter_root.exists() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "castord state root is not fresh",
            ));
        }
        let effect_adapter = D1EffectAdapter::initialize(
            &adapter_root,
            &core_root,
            adapter_id,
            assurance_profile,
            provider,
        )?;
        let storage = D1DurableStorage::open(&core_root)?;
        Ok(Self {
            state_root: fs::canonicalize(state_root)?,
            storage,
            effect_adapter,
        })
    }

    pub fn open(
        state_root: impl AsRef<Path>,
        adapter_id: &str,
        assurance_profile: &str,
        provider: P,
    ) -> io::Result<Self> {
        let state_root = fs::canonicalize(state_root.as_ref())?;
        let core_root = state_root.join("core");
        let adapter_root = state_root.join("adapter");
        let storage = D1DurableStorage::open(&core_root)?;
        let effect_adapter = D1EffectAdapter::open(
            &adapter_root,
            &core_root,
            adapter_id,
            assurance_profile,
            provider,
        )?;
        Ok(Self {
            state_root,
            storage,
            effect_adapter,
        })
    }

    pub fn state_root(&self) -> &Path {
        &self.state_root
    }

    pub fn storage_mut(&mut self) -> &mut D1DurableStorage {
        &mut self.storage
    }

    pub fn effect_adapter_mut(&mut self) -> &mut D1EffectAdapter<P> {
        &mut self.effect_adapter
    }
}
