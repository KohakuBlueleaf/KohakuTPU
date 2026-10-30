//! Bounded XDMA completion polling into one page-aligned staging buffer.

use super::exchange::QueueLayout;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

#[cfg(unix)]
mod posix {
    use super::QueueLayout;
    use std::ffi::{c_int, c_void};
    use std::io;
    use std::time::{Duration, Instant};

    unsafe extern "C" {
        fn pread(fd: c_int, buf: *mut c_void, count: usize, offset: i64) -> isize;
    }
    #[repr(align(4096))]
    struct Page([u8; 4096]);

    #[allow(clippy::too_many_arguments)]
    pub fn wait(
        fd: i32,
        address: i64,
        head: u64,
        stdout: u64,
        seconds: f64,
        sleep: f64,
        max_polls: u64,
        layout: &QueueLayout,
        cq_only: bool,
    ) -> io::Result<(Vec<u8>, u64)> {
        let mut page = Page([0; 4096]);
        let span = (layout.so_wr + layout.line as u64 - layout.fw) as usize;
        let cq_tail_rel = (layout.cq_tail - layout.fw) as usize;
        let so_wr_rel = (layout.so_wr - layout.fw) as usize;
        let start = Instant::now();
        let duration = Duration::from_secs_f64(seconds);
        let delay = Duration::from_secs_f64(sleep);
        let mut polls = 0;
        loop {
            // FW through SO_WR: the tail is observed before any CQ slot is read.
            let got = unsafe { pread(fd, page.0.as_mut_ptr().cast(), span, address) };
            if got < 0 {
                return Err(io::Error::last_os_error());
            }
            if got as usize != span {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    format!("short queue read: {got} of {span}"),
                ));
            }
            polls += 1;
            let word = |at| u64::from_le_bytes(page.0[at..at + 8].try_into().unwrap());
            if word(cq_tail_rel) != head
                || (!cq_only && word(so_wr_rel) != stdout)
                || (!cq_only && word(layout.fw_state * 8) == layout.st_fatal)
                || start.elapsed() >= duration
                || (max_polls != 0 && polls >= max_polls)
            {
                break;
            }
            if !delay.is_zero() {
                std::thread::sleep(delay);
            }
        }
        Ok((page.0[..span].to_vec(), polls))
    }
}

#[pyfunction]
#[pyo3(signature=(fd,address,head,stdout,seconds=0.0001,sleep=0.0,max_polls=0,layout=None))]
#[allow(clippy::too_many_arguments)]
fn wait_tail<'py>(
    py: Python<'py>,
    fd: i32,
    address: i64,
    head: u64,
    stdout: u64,
    seconds: f64,
    sleep: f64,
    max_polls: u64,
    layout: Option<&QueueLayout>,
) -> PyResult<(Bound<'py, PyBytes>, u64)> {
    if !seconds.is_finite()
        || !sleep.is_finite()
        || !(0.0..=0.05).contains(&seconds)
        || !(0.0..=0.01).contains(&sleep)
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "bounded polling seconds 0..=.05 and sleep 0..=.01 required",
        ));
    }
    let layout = layout.ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err("wait_tail requires a QueueLayout")
    })?;
    #[cfg(unix)]
    {
        let cq_only = mutant_on!(py, "bindings.poll/cq_only");
        // The caller owns fd. Keeping the GIL prevents concurrent Python close/reopen.
        let (data, polls) = posix::wait(
            fd, address, head, stdout, seconds, sleep, max_polls, layout, cq_only,
        )?;
        Ok((PyBytes::new(py, &data), polls))
    }
    #[cfg(not(unix))]
    {
        let _ = (py, fd, address, head, stdout, max_polls, layout);
        Err(pyo3::exceptions::PyRuntimeError::new_err(
            "native XDMA poll requires POSIX pread",
        ))
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(wait_tail, m)?)
}
