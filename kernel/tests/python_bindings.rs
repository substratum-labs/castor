use castor_kernel::register_python_module;
use pyo3::exceptions::PyPermissionError;
use pyo3::prelude::*;
use pyo3::types::PyModule;

fn runtime_module<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyModule>> {
    let module = PyModule::new(py, "castor_kernel")?;
    register_python_module(&module)?;
    Ok(module)
}

#[test]
fn python_runtime_commits_an_authorized_effect_and_reports_its_journal() {
    Python::initialize();
    Python::attach(|py| -> PyResult<()> {
        let module = runtime_module(py)?;
        let runtime = module.getattr("KernelRuntime")?.call0()?;

        runtime.call_method1("grant", ("cap_a",))?;
        runtime.call_method1("propose", ("effect_a",))?;
        runtime.call_method1("commit", ("effect_a",))?;

        assert_eq!(
            runtime.getattr("agent_state")?.extract::<String>()?,
            "running"
        );
        assert_eq!(runtime.getattr("cursor")?.extract::<usize>()?, 2);
        assert_eq!(
            runtime
                .getattr("journal")?
                .extract::<Vec<(String, String)>>()?,
            vec![
                ("proposed".to_owned(), "effect_a".to_owned()),
                ("committed".to_owned(), "effect_a".to_owned()),
            ],
        );
        Ok(())
    })
    .unwrap();
}

#[test]
fn python_runtime_maps_revoked_capability_commit_to_permission_error() {
    Python::initialize();
    Python::attach(|py| -> PyResult<()> {
        let module = runtime_module(py)?;
        let runtime = module.getattr("KernelRuntime")?.call0()?;

        runtime.call_method1("grant", ("cap_a",))?;
        runtime.call_method1("propose", ("effect_a",))?;
        runtime.call_method1("revoke", ("cap_a",))?;

        let error = runtime.call_method1("commit", ("effect_a",)).unwrap_err();
        assert!(error.is_instance_of::<PyPermissionError>(py));
        Ok(())
    })
    .unwrap();
}

#[test]
fn python_runtime_exposes_rejection_preemption_and_resumption() {
    Python::initialize();
    Python::attach(|py| -> PyResult<()> {
        let module = runtime_module(py)?;

        let rejected = module.getattr("KernelRuntime")?.call0()?;
        rejected.call_method1("grant", ("cap_a",))?;
        rejected.call_method1("propose", ("effect_a",))?;
        rejected.call_method1("reject", ("effect_a",))?;
        assert_eq!(
            rejected.getattr("agent_state")?.extract::<String>()?,
            "running"
        );
        assert_eq!(rejected.getattr("cursor")?.extract::<usize>()?, 2);

        let resumed = module.getattr("KernelRuntime")?.call0()?;
        resumed.call_method0("preempt")?;
        assert_eq!(
            resumed.getattr("agent_state")?.extract::<String>()?,
            "suspended"
        );
        resumed.call_method0("resume")?;
        assert_eq!(
            resumed.getattr("agent_state")?.extract::<String>()?,
            "running"
        );
        Ok(())
    })
    .unwrap();
}
