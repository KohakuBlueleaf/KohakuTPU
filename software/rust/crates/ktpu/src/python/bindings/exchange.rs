//! Callback-free publication and terminal collection of one retained RUN/LIST.
//! The native owner duplicates the descriptors; releasing the GIL never borrows
//! handles which Python transport recovery can close/reassign underneath it.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

fn item<'py, T>(dict: &Bound<'py, PyDict>, key: &str) -> PyResult<T>
where
    T: for<'a> FromPyObject<'a, 'py>,
    for<'a> <T as FromPyObject<'a, 'py>>::Error: Into<PyErr>,
{
    dict.get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("missing queue schema key {key:?}")))?
        .extract()
        .map_err(Into::into)
}

fn sub<'py>(parent: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyDict>> {
    parent
        .get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("missing queue schema key {key:?}")))?
        .cast_into::<PyDict>()
        .map_err(Into::into)
}

fn map_val<V: Copy>(map: &std::collections::BTreeMap<String, V>, key: &str) -> PyResult<V> {
    map.get(key)
        .copied()
        .ok_or_else(|| PyValueError::new_err(format!("missing queue schema key {key:?}")))
}

#[pyclass(module = "kohakutpu.compiler._rs", get_all)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct QueueLayout {
    pub magic: u64,
    pub min_version: u32,
    pub version: u32,
    pub list_version: u32,
    pub line: usize,
    pub sq_bytes: usize,
    pub cq_bytes: usize,
    pub sq_words: usize,
    pub nbind_shift: u32,
    pub stdio_mask: u64,
    pub op_mask: u64,
    pub cq_status_mask: u64,
    pub rec_bytes: usize,
    pub rec_version: u32,
    pub rec_word: usize,
    pub info: u64,
    pub epoch: u64,
    pub fw: u64,
    pub list_abi: u64,
    pub sq_tail: u64,
    pub sq_head: u64,
    pub cq_tail: u64,
    pub cq_head: u64,
    pub so_wr: u64,
    pub so_rd: u64,
    pub si_wr: u64,
    pub si_rd: u64,
    pub units: u64,
    pub heaps: u64,
    pub sq: u64,
    pub cq_tag: usize,
    pub cq_status: usize,
    pub cq_cycles: usize,
    pub cq_value: usize,
    pub info_magic: usize,
    pub info_version: usize,
    pub info_rings: usize,
    pub info_stdio: usize,
    pub info_epoch: usize,
    pub fw_state: usize,
    pub fw_abi: usize,
    pub fw_done: usize,
    pub fw_fatal: usize,
    pub st_none: u64,
    pub st_ready: u64,
    pub st_stopped: u64,
    pub st_fatal: u64,
    pub op_nop: u64,
    pub op_run: u64,
    pub op_stop: u64,
    pub op_list: u64,
    pub run_op: usize,
    pub run_tag: usize,
    pub run_pkg: usize,
    pub run_bytes: usize,
    pub run_bind: usize,
    pub run_timeout: usize,
    pub run_flags: usize,
    pub list_op: usize,
    pub list_tag: usize,
    pub list_addr: usize,
    pub list_count: usize,
    pub list_epoch: usize,
    pub list_version_word: usize,
    pub list_flags: usize,
    pub status_ok: u64,
    pub status_progress: u64,
    pub status_bad_op: u64,
    pub status_bad_arg: u64,
    pub status_unit_fault: u64,
    pub status_mover_fault: u64,
    pub status_timeout: u64,
    pub status_no_memory: u64,
    pub status_no_slots: u64,
    pub status_bad_free: u64,
    pub status_heap_busy: u64,
    pub rec_tag: usize,
    pub rec_so_wr: usize,
    pub rec_list_index: usize,
    pub rec_item_tag: usize,
    pub rec_epoch: usize,
}

impl QueueLayout {
    pub(crate) fn from_queue(q: &ktpu_machine::schema::firmware::Queue) -> PyResult<Self> {
        let rf = |name: &str| -> PyResult<usize> {
            q.record
                .fields
                .get(name)
                .map(|f| f.offset as usize)
                .ok_or_else(|| PyValueError::new_err(format!("missing record field {name:?}")))
        };
        Ok(Self {
            magic: q.magic,
            min_version: q.min_version,
            version: q.version,
            list_version: q.list_version,
            line: q.line as usize,
            sq_bytes: q.sq_bytes as usize,
            cq_bytes: q.cq_bytes as usize,
            sq_words: q.sq_words as usize,
            nbind_shift: q.nbind_shift,
            stdio_mask: q.stdio_mask,
            op_mask: q.op_mask as u64,
            cq_status_mask: q.cq_status_mask as u64,
            rec_bytes: q.record.bytes as usize,
            rec_version: q.record.version,
            rec_word: q.record.word as usize,
            info: map_val(&q.offsets, "info")? as u64,
            epoch: map_val(&q.offsets, "epoch")? as u64,
            fw: map_val(&q.offsets, "fw")? as u64,
            list_abi: map_val(&q.offsets, "list_abi")? as u64,
            sq_tail: map_val(&q.offsets, "sq_tail")? as u64,
            sq_head: map_val(&q.offsets, "sq_head")? as u64,
            cq_tail: map_val(&q.offsets, "cq_tail")? as u64,
            cq_head: map_val(&q.offsets, "cq_head")? as u64,
            so_wr: map_val(&q.offsets, "so_wr")? as u64,
            so_rd: map_val(&q.offsets, "so_rd")? as u64,
            si_wr: map_val(&q.offsets, "si_wr")? as u64,
            si_rd: map_val(&q.offsets, "si_rd")? as u64,
            units: map_val(&q.offsets, "units")? as u64,
            heaps: map_val(&q.offsets, "heaps")? as u64,
            sq: map_val(&q.offsets, "sq")? as u64,
            cq_tag: map_val(&q.cq_offsets, "tag")? as usize,
            cq_status: map_val(&q.cq_offsets, "status")? as usize,
            cq_cycles: map_val(&q.cq_offsets, "cycles")? as usize,
            cq_value: map_val(&q.cq_offsets, "value")? as usize,
            info_magic: map_val(&q.info_words, "magic")? as usize,
            info_version: map_val(&q.info_words, "version")? as usize,
            info_rings: map_val(&q.info_words, "rings")? as usize,
            info_stdio: map_val(&q.info_words, "stdio")? as usize,
            info_epoch: map_val(&q.info_words, "epoch")? as usize,
            fw_state: map_val(&q.fw_words, "state")? as usize,
            fw_abi: map_val(&q.fw_words, "abi")? as usize,
            fw_done: map_val(&q.fw_words, "done")? as usize,
            fw_fatal: map_val(&q.fw_words, "fatal")? as usize,
            st_none: map_val(&q.states, "none")? as u64,
            st_ready: map_val(&q.states, "ready")? as u64,
            st_stopped: map_val(&q.states, "stopped")? as u64,
            st_fatal: map_val(&q.states, "fatal")? as u64,
            op_nop: map_val(&q.opcodes, "nop")? as u64,
            op_run: map_val(&q.opcodes, "run")? as u64,
            op_stop: map_val(&q.opcodes, "stop")? as u64,
            op_list: map_val(&q.opcodes, "list")? as u64,
            run_op: map_val(&q.run_words, "op")? as usize,
            run_tag: map_val(&q.run_words, "tag")? as usize,
            run_pkg: map_val(&q.run_words, "pkg")? as usize,
            run_bytes: map_val(&q.run_words, "bytes")? as usize,
            run_bind: map_val(&q.run_words, "bind")? as usize,
            run_timeout: map_val(&q.run_words, "timeout")? as usize,
            run_flags: map_val(&q.run_words, "flags")? as usize,
            list_op: map_val(&q.list_words, "op")? as usize,
            list_tag: map_val(&q.list_words, "tag")? as usize,
            list_addr: map_val(&q.list_words, "addr")? as usize,
            list_count: map_val(&q.list_words, "count")? as usize,
            list_epoch: map_val(&q.list_words, "epoch")? as usize,
            list_version_word: map_val(&q.list_words, "version")? as usize,
            list_flags: map_val(&q.list_words, "flags")? as usize,
            status_ok: map_val(&q.status, "ok")? as u64,
            status_progress: map_val(&q.status, "progress")? as u64,
            status_bad_op: map_val(&q.status, "bad_op")? as u64,
            status_bad_arg: map_val(&q.status, "bad_arg")? as u64,
            status_unit_fault: map_val(&q.status, "unit_fault")? as u64,
            status_mover_fault: map_val(&q.status, "mover_fault")? as u64,
            status_timeout: map_val(&q.status, "timeout")? as u64,
            status_no_memory: map_val(&q.status, "no_memory")? as u64,
            status_no_slots: map_val(&q.status, "no_slots")? as u64,
            status_bad_free: map_val(&q.status, "bad_free")? as u64,
            status_heap_busy: map_val(&q.status, "heap_busy")? as u64,
            rec_tag: rf("tag")?,
            rec_so_wr: rf("so_wr")?,
            rec_list_index: rf("list_index")?,
            rec_item_tag: rf("item_tag")?,
            rec_epoch: rf("epoch")?,
        })
    }

    pub(crate) fn from_dict(q: &Bound<'_, PyDict>) -> PyResult<Self> {
        let offsets = sub(q, "offsets")?;
        let cq_offsets = sub(q, "cq_offsets")?;
        let info_words = sub(q, "info_words")?;
        let fw_words = sub(q, "fw_words")?;
        let states = sub(q, "states")?;
        let opcodes = sub(q, "opcodes")?;
        let run_words = sub(q, "run_words")?;
        let list_words = sub(q, "list_words")?;
        let status = sub(q, "status")?;
        let record = sub(q, "record")?;
        let fields = sub(&record, "fields")?;
        let rf = |name: &str| -> PyResult<usize> {
            let entry = fields
                .get_item(name)?
                .ok_or_else(|| PyValueError::new_err(format!("missing record field {name:?}")))?;
            if let Ok(d) = entry.cast::<PyDict>() {
                item(d, "offset")
            } else {
                let (offset, _): (usize, usize) = entry.extract()?;
                Ok(offset)
            }
        };
        Ok(Self {
            magic: item(q, "magic")?,
            min_version: item(q, "min_version")?,
            version: item(q, "version")?,
            list_version: item(q, "list_version")?,
            line: item(q, "line")?,
            sq_bytes: item(q, "sq_bytes")?,
            cq_bytes: item(q, "cq_bytes")?,
            sq_words: item(q, "sq_words")?,
            nbind_shift: item(q, "nbind_shift")?,
            stdio_mask: item(q, "stdio_mask")?,
            op_mask: item(q, "op_mask")?,
            cq_status_mask: item(q, "cq_status_mask")?,
            rec_bytes: item(&record, "bytes")?,
            rec_version: item(&record, "version")?,
            rec_word: item(&record, "word")?,
            info: item(&offsets, "info")?,
            epoch: item(&offsets, "epoch")?,
            fw: item(&offsets, "fw")?,
            list_abi: item(&offsets, "list_abi")?,
            sq_tail: item(&offsets, "sq_tail")?,
            sq_head: item(&offsets, "sq_head")?,
            cq_tail: item(&offsets, "cq_tail")?,
            cq_head: item(&offsets, "cq_head")?,
            so_wr: item(&offsets, "so_wr")?,
            so_rd: item(&offsets, "so_rd")?,
            si_wr: item(&offsets, "si_wr")?,
            si_rd: item(&offsets, "si_rd")?,
            units: item(&offsets, "units")?,
            heaps: item(&offsets, "heaps")?,
            sq: item(&offsets, "sq")?,
            cq_tag: item(&cq_offsets, "tag")?,
            cq_status: item(&cq_offsets, "status")?,
            cq_cycles: item(&cq_offsets, "cycles")?,
            cq_value: item(&cq_offsets, "value")?,
            info_magic: item(&info_words, "magic")?,
            info_version: item(&info_words, "version")?,
            info_rings: item(&info_words, "rings")?,
            info_stdio: item(&info_words, "stdio")?,
            info_epoch: item(&info_words, "epoch")?,
            fw_state: item(&fw_words, "state")?,
            fw_abi: item(&fw_words, "abi")?,
            fw_done: item(&fw_words, "done")?,
            fw_fatal: item(&fw_words, "fatal")?,
            st_none: item(&states, "none")?,
            st_ready: item(&states, "ready")?,
            st_stopped: item(&states, "stopped")?,
            st_fatal: item(&states, "fatal")?,
            op_nop: item(&opcodes, "nop")?,
            op_run: item(&opcodes, "run")?,
            op_stop: item(&opcodes, "stop")?,
            op_list: item(&opcodes, "list")?,
            run_op: item(&run_words, "op")?,
            run_tag: item(&run_words, "tag")?,
            run_pkg: item(&run_words, "pkg")?,
            run_bytes: item(&run_words, "bytes")?,
            run_bind: item(&run_words, "bind")?,
            run_timeout: item(&run_words, "timeout")?,
            run_flags: item(&run_words, "flags")?,
            list_op: item(&list_words, "op")?,
            list_tag: item(&list_words, "tag")?,
            list_addr: item(&list_words, "addr")?,
            list_count: item(&list_words, "count")?,
            list_epoch: item(&list_words, "epoch")?,
            list_version_word: item(&list_words, "version")?,
            list_flags: item(&list_words, "flags")?,
            status_ok: item(&status, "ok")?,
            status_progress: item(&status, "progress")?,
            status_bad_op: item(&status, "bad_op")?,
            status_bad_arg: item(&status, "bad_arg")?,
            status_unit_fault: item(&status, "unit_fault")?,
            status_mover_fault: item(&status, "mover_fault")?,
            status_timeout: item(&status, "timeout")?,
            status_no_memory: item(&status, "no_memory")?,
            status_no_slots: item(&status, "no_slots")?,
            status_bad_free: item(&status, "bad_free")?,
            status_heap_busy: item(&status, "heap_busy")?,
            rec_tag: rf("tag")?,
            rec_so_wr: rf("so_wr")?,
            rec_list_index: rf("list_index")?,
            rec_item_tag: rf("item_tag")?,
            rec_epoch: rf("epoch")?,
        })
    }

    pub(crate) fn from_py(obj: &Bound<'_, PyAny>) -> PyResult<Self> {
        if let Ok(layout) = obj.extract::<PyRef<'_, QueueLayout>>() {
            return Ok(layout.clone());
        }
        if let Ok(json) = obj.extract::<&str>() {
            let m = super::machine(json)?;
            return Self::from_queue(&m.fw.queue);
        }
        if obj.hasattr("schema")? {
            let schema = obj.getattr("schema")?;
            let dict = schema.cast::<PyDict>()?;
            return Self::from_dict(dict);
        }
        if obj.hasattr("layout")? {
            let inner = obj.getattr("layout")?;
            return Self::from_py(&inner);
        }
        if let Ok(dict) = obj.cast::<PyDict>() {
            if let Some(m) = dict.get_item("machine")? {
                let m = m.cast_into::<PyDict>()?;
                let fw = sub(&m, "fw")?;
                let q = sub(&fw, "queue")?;
                return Self::from_dict(&q);
            }
            return Self::from_dict(dict);
        }
        Err(PyValueError::new_err(
            "expected QueueLayout, machine JSON, or queue schema",
        ))
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn run_entry(
        &self,
        tag: u64,
        pkg: u64,
        nbytes: u64,
        bind: u64,
        nbind: u64,
        timeout: u64,
        flags: u64,
    ) -> Vec<u64> {
        let mut words = vec![0u64; self.sq_words];
        words[self.run_op] = self.op_run | (nbind << self.nbind_shift);
        words[self.run_tag] = tag;
        words[self.run_pkg] = pkg;
        words[self.run_bytes] = nbytes;
        words[self.run_bind] = bind;
        words[self.run_timeout] = timeout;
        words[self.run_flags] = flags;
        words
    }
}

#[pymethods]
impl QueueLayout {
    #[new]
    fn new(source: &Bound<'_, PyAny>) -> PyResult<Self> {
        Self::from_py(source)
    }
}

#[derive(Default)]
pub(crate) struct Outcome {
    pub head: u64,
    pub stdout: u64,
    pub record: Vec<u8>,
    pub completion: Vec<u8>,
    pub console: Vec<u8>,
    pub polls: u64,
    pub published: bool,
    pub error: Option<String>,
}

#[cfg(unix)]
pub(crate) mod posix {
    use super::{Outcome, QueueLayout};
    use std::ffi::{c_int, c_void};
    use std::fs::File;
    use std::io;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::time::{Duration, Instant};

    unsafe extern "C" {
        fn dup(fd: c_int) -> c_int;
        fn pread(fd: c_int, buf: *mut c_void, count: usize, offset: i64) -> isize;
        fn pwrite(fd: c_int, buf: *const c_void, count: usize, offset: i64) -> isize;
    }
    #[repr(align(4096))]
    struct Page([u8; 4096]);

    pub struct Io {
        read: File,
        write: File,
    }
    fn duplicate(fd: i32) -> io::Result<File> {
        let copied = unsafe { dup(fd) };
        if copied < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { File::from_raw_fd(copied) })
    }
    fn word(raw: &[u8], off: usize) -> u64 {
        u64::from_le_bytes(raw[off..off + 8].try_into().unwrap())
    }
    impl Io {
        pub fn new(read: i32, write: i32) -> io::Result<Self> {
            Ok(Self {
                read: duplicate(read)?,
                write: duplicate(write)?,
            })
        }
        fn read(&self, at: u64, n: usize, line: usize) -> io::Result<Vec<u8>> {
            let mut raw = Page([0; 4096]);
            let mut out = Vec::with_capacity(n);
            while out.len() < n {
                let position = at + out.len() as u64;
                let skip = (position % line as u64) as usize;
                let take = (n - out.len()).min(4096 - skip);
                let span = (skip + take).div_ceil(line) * line;
                let got = unsafe {
                    pread(
                        self.read.as_raw_fd(),
                        raw.0.as_mut_ptr().cast(),
                        span,
                        (position - skip as u64) as i64,
                    )
                };
                if got < 0 {
                    return Err(io::Error::last_os_error());
                }
                if got as usize != span {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "short native queue read",
                    ));
                }
                out.extend_from_slice(&raw.0[skip..skip + take]);
            }
            Ok(out)
        }
        pub fn upload(&self, at: u64, bytes: &[u8], line: usize) -> io::Result<()> {
            for (i, chunk) in bytes.chunks(4096).enumerate() {
                let mut padded = Page([0; 4096]);
                padded.0[..chunk.len()].copy_from_slice(chunk);
                self.write(
                    at + (i * 4096) as u64,
                    &padded.0[..chunk.len().div_ceil(line) * line],
                )?;
            }
            Ok(())
        }
        fn write(&self, at: u64, bytes: &[u8]) -> io::Result<()> {
            let mut raw = Page([0; 4096]);
            raw.0[..bytes.len()].copy_from_slice(bytes);
            let got = unsafe {
                pwrite(
                    self.write.as_raw_fd(),
                    raw.0.as_ptr().cast(),
                    bytes.len(),
                    at as i64,
                )
            };
            if got < 0 {
                return Err(io::Error::last_os_error());
            }
            if got as usize != bytes.len() {
                return Err(io::Error::new(
                    io::ErrorKind::WriteZero,
                    "short native queue write",
                ));
            }
            Ok(())
        }
        fn ack(&self, at: u64, value: u64, line: usize) -> io::Result<()> {
            let mut raw = vec![0u8; line];
            raw[..8].copy_from_slice(&value.to_le_bytes());
            self.write(at, &raw)
        }
        #[allow(clippy::too_many_arguments)]
        pub fn exchange(
            &self,
            layout: &QueueLayout,
            base: u64,
            geo: [u64; 5],
            tail: u64,
            head: u64,
            stdout: u64,
            entry: &[u64],
            seconds: f64,
        ) -> Outcome {
            let mut out = Outcome {
                head,
                stdout,
                ..Outcome::default()
            };
            let mut run = || -> io::Result<()> {
                let [sq_n, cq_n, cq, rec, so] = geo;
                let line = layout.line;
                let poll_span = (layout.so_wr + line as u64 - layout.fw) as usize;
                let sq_head_rel = (layout.sq_head - layout.fw) as usize;
                let cq_tail_rel = (layout.cq_tail - layout.fw) as usize;
                let so_wr_rel = (layout.so_wr - layout.fw) as usize;
                let start = Instant::now();
                let deadline = Duration::from_secs_f64(seconds);
                loop {
                    let fw = self.read(base + layout.fw, poll_span, line)?;
                    out.polls += 1;
                    if word(&fw, layout.fw_state * 8) == layout.st_fatal {
                        return Err(io::Error::other("fatal firmware state"));
                    }
                    let sq_head = word(&fw, sq_head_rel);
                    if tail - sq_head < sq_n {
                        break;
                    }
                    if start.elapsed() >= deadline {
                        return Err(io::Error::new(io::ErrorKind::TimedOut, "native SQ timeout"));
                    }
                }
                let mut raw = vec![0u8; layout.sq_bytes];
                for (i, value) in entry.iter().enumerate() {
                    raw[i * 8..i * 8 + 8].copy_from_slice(&value.to_le_bytes());
                }
                // Publication uncertainty begins BEFORE either potentially partial write.
                out.published = true;
                self.write(
                    base + layout.sq + (tail % sq_n) * layout.sq_bytes as u64,
                    &raw,
                )?;
                self.ack(base + layout.sq_tail, tail + 1, line)?;
                loop {
                    let fw = self.read(base + layout.fw, poll_span, line)?;
                    out.polls += 1;
                    if word(&fw, layout.fw_state * 8) == layout.st_fatal {
                        return Err(io::Error::other("fatal firmware state"));
                    }
                    let cq_tail = word(&fw, cq_tail_rel);
                    if cq_tail < out.head || cq_tail - out.head > cq_n {
                        return Err(io::Error::other("native CQ overflow"));
                    }
                    let info = self.read(base + layout.info, line, line)?;
                    let so_n = word(&info, layout.info_stdio * 8) & layout.stdio_mask;
                    let so_tail = word(&fw, so_wr_rel);
                    if so_tail < out.stdout || so_tail - out.stdout > so_n {
                        return Err(io::Error::other("native stdout overflow"));
                    }
                    let before = out.stdout;
                    while out.stdout < so_tail {
                        let slot = out.stdout % so_n;
                        let aligned = slot & !7;
                        let skip = (slot - aligned) as usize;
                        let n = (so_tail - out.stdout)
                            .min(so_n - slot)
                            .min(4096 - skip as u64) as usize;
                        let bytes =
                            self.read(base + so + aligned, (skip + n).div_ceil(8) * 8, line)?;
                        out.console.extend_from_slice(&bytes[skip..skip + n]);
                        out.stdout += n as u64;
                    }
                    if before != out.stdout {
                        self.ack(base + layout.so_rd, out.stdout, line)?;
                    }
                    while out.head < cq_tail {
                        let slot = out.head % cq_n;
                        let c = self.read(
                            base + cq + slot * layout.cq_bytes as u64,
                            layout.cq_bytes,
                            line,
                        )?;
                        let r = self.read(
                            base + rec + slot * layout.rec_bytes as u64,
                            layout.rec_bytes,
                            line,
                        )?;
                        let is_list = (entry[layout.run_op] & layout.op_mask) == layout.op_list;
                        let tag_idx = if is_list {
                            layout.list_tag
                        } else {
                            layout.run_tag
                        };
                        if word(&c, layout.cq_tag) != entry[tag_idx]
                            || word(&r, layout.rec_tag) != entry[tag_idx]
                        {
                            return Err(io::Error::other("native completion tag mismatch"));
                        }
                        let status = word(&c, layout.cq_status) & layout.cq_status_mask;
                        if is_list && word(&r, layout.rec_epoch) != entry[layout.list_epoch] {
                            return Err(io::Error::other("native completion epoch mismatch"));
                        }
                        self.ack(base + layout.cq_head, out.head + 1, line)?;
                        out.head += 1;
                        if status != layout.status_progress {
                            out.completion = c;
                            out.record = r;
                            return Ok(());
                        }
                    }
                    if start.elapsed() >= deadline {
                        return Err(io::Error::new(io::ErrorKind::TimedOut, "native CQ timeout"));
                    }
                }
            };
            if let Err(error) = run() {
                out.error = Some(error.to_string());
            }
            out
        }
    }
}

#[pyclass(module = "kohakutpu.compiler._rs")]
struct QueueExchange {
    layout: QueueLayout,
    #[cfg(unix)]
    io: posix::Io,
}
#[pymethods]
impl QueueExchange {
    #[new]
    fn new(read: i32, write: i32, layout: &Bound<'_, PyAny>) -> PyResult<Self> {
        let layout = QueueLayout::from_py(layout)?;
        #[cfg(unix)]
        {
            Ok(Self {
                layout,
                io: posix::Io::new(read, write)?,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = (read, write, layout);
            Err(pyo3::exceptions::PyRuntimeError::new_err(
                "native queue exchange needs POSIX descriptors",
            ))
        }
    }
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    fn run<'py>(
        &self,
        py: Python<'py>,
        base: u64,
        geo: [u64; 5],
        tail: u64,
        head: u64,
        stdout: u64,
        entry: Vec<u64>,
        seconds: f64,
    ) -> PyResult<(
        u64,
        u64,
        Bound<'py, PyBytes>,
        Bound<'py, PyBytes>,
        Bound<'py, PyBytes>,
        u64,
        bool,
        Option<String>,
    )> {
        if std::time::Duration::try_from_secs_f64(seconds).is_err()
            || seconds <= 0.0
            || geo[0] == 0
            || geo[1] == 0
            || entry.len() != self.layout.sq_words
        {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "positive ring sizes and representable positive timeout required",
            ));
        }
        #[cfg(unix)]
        let out = py.detach(|| {
            self.io
                .exchange(&self.layout, base, geo, tail, head, stdout, &entry, seconds)
        });
        #[cfg(not(unix))]
        let out = Outcome {
            error: Some("unsupported transport".into()),
            ..Outcome::default()
        };
        Ok((
            out.head,
            out.stdout,
            PyBytes::new(py, &out.completion),
            PyBytes::new(py, &out.record),
            PyBytes::new(py, &out.console),
            out.polls,
            out.published,
            out.error,
        ))
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<QueueLayout>()?;
    m.add_class::<QueueExchange>()
}
