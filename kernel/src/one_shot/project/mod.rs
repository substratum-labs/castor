pub mod archive;
pub mod engine;
pub mod git;
pub mod io;
pub mod receipt;
pub mod spec;

use std::io::{self as stdio, ErrorKind};

pub(crate) fn invalid(message: impl Into<String>) -> stdio::Error {
    stdio::Error::new(ErrorKind::InvalidInput, message.into())
}
