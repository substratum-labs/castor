use pyo3::exceptions::{PyPermissionError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;

use crate::runtime::{
    fault_preempt, grant_capability, resume_execution, revoke_capability, syscall_commit,
    syscall_propose, syscall_reject, AgentState, Capability, Effect, JournalRecordType,
    KernelState, SyscallError,
};

pub mod c01_storage;
pub mod c02_execution;
pub mod c04_adapter;
pub mod c05_settlement;
pub mod castord;
pub mod runtime;
pub mod spec;

#[pyclass]
pub struct KernelRuntime {
    state: KernelState,
}

fn parse_effect(effect: &str) -> PyResult<Effect> {
    match effect {
        "effect_a" => Ok(Effect::EffectA),
        "effect_b" => Ok(Effect::EffectB),
        _ => Err(PyValueError::new_err(
            "effect must be 'effect_a' or 'effect_b'",
        )),
    }
}

fn parse_capability(capability: &str) -> PyResult<Capability> {
    match capability {
        "cap_a" => Ok(Capability::CapA),
        "cap_b" => Ok(Capability::CapB),
        _ => Err(PyValueError::new_err(
            "capability must be 'cap_a' or 'cap_b'",
        )),
    }
}

fn effect_name(effect: Effect) -> &'static str {
    match effect {
        Effect::EffectA => "effect_a",
        Effect::EffectB => "effect_b",
    }
}

fn record_type_name(record_type: JournalRecordType) -> &'static str {
    match record_type {
        JournalRecordType::Proposed => "proposed",
        JournalRecordType::Committed => "committed",
        JournalRecordType::Rejected => "rejected",
    }
}

fn agent_state_name(agent_state: AgentState) -> &'static str {
    match agent_state {
        AgentState::Running => "running",
        AgentState::PendingHitl => "pending_hitl",
        AgentState::Suspended => "suspended",
    }
}

fn syscall_error(error: SyscallError) -> PyErr {
    match error {
        SyscallError::Unauthorized => PyPermissionError::new_err("capability is not authorized"),
        SyscallError::InvalidState => PyRuntimeError::new_err("syscall is invalid in this state"),
    }
}

#[pymethods]
impl KernelRuntime {
    #[new]
    fn new() -> Self {
        Self {
            state: KernelState::new(),
        }
    }

    fn grant(&mut self, capability: &str) -> PyResult<()> {
        self.state = grant_capability(self.state.clone(), parse_capability(capability)?);
        Ok(())
    }

    fn revoke(&mut self, capability: &str) -> PyResult<()> {
        self.state = revoke_capability(self.state.clone(), parse_capability(capability)?);
        Ok(())
    }

    fn propose(&mut self, effect: &str) -> PyResult<()> {
        self.state =
            syscall_propose(self.state.clone(), parse_effect(effect)?).map_err(syscall_error)?;
        Ok(())
    }

    fn commit(&mut self, effect: &str) -> PyResult<()> {
        self.state =
            syscall_commit(self.state.clone(), parse_effect(effect)?).map_err(syscall_error)?;
        Ok(())
    }

    fn reject(&mut self, effect: &str) -> PyResult<()> {
        self.state =
            syscall_reject(self.state.clone(), parse_effect(effect)?).map_err(syscall_error)?;
        Ok(())
    }

    fn preempt(&mut self) {
        self.state = fault_preempt(self.state.clone());
    }

    fn resume(&mut self) -> PyResult<()> {
        self.state = resume_execution(self.state.clone()).map_err(syscall_error)?;
        Ok(())
    }

    #[getter]
    fn agent_state(&self) -> &'static str {
        agent_state_name(self.state.agent_state)
    }

    #[getter]
    fn cursor(&self) -> usize {
        self.state.cursor
    }

    #[getter]
    fn journal(&self) -> Vec<(&'static str, &'static str)> {
        self.state
            .journal
            .iter()
            .map(|entry| {
                (
                    record_type_name(entry.record_type),
                    effect_name(entry.effect),
                )
            })
            .collect()
    }
}

/// Formats the sum of two numbers as string.
#[pyfunction]
fn sum_as_string(a: usize, b: usize) -> PyResult<String> {
    Ok((a + b).to_string())
}

pub fn register_python_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<KernelRuntime>()?;
    m.add_function(wrap_pyfunction!(sum_as_string, m)?)?;
    Ok(())
}

/// A Python module implemented in Rust for Castor Microkernel.
#[pymodule]
fn castor_kernel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    register_python_module(m)
}
