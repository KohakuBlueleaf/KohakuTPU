//! Per-stage on-disk caches: an entry is a stage's output bytes, keyed on
//! sha256(stage name, stage version, machine digest, input bytes), under
//! `<root>/<stage>/<key[0..2]>/<key>.bin` (`root` is `build/rcache`), so a
//! change to one stage keeps the other stages' entries. A write goes to a
//! temporary file in the entry's directory and is renamed into place, so a
//! reader sees a whole entry or none.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use sha2::{Digest, Sha256};

/// An entry's key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Key(pub [u8; 32]);

impl Key {
    /// sha256 over the four fields, each as its byte length (u64
    /// little-endian) then its bytes, so no two field splits collide.
    pub fn of(stage: &str, version: &str, machine_digest: &[u8], input: &[u8]) -> Key {
        let mut h = Sha256::new();
        for part in [stage.as_bytes(), version.as_bytes(), machine_digest, input] {
            h.update((part.len() as u64).to_le_bytes());
            h.update(part);
        }
        Key(h.finalize().into())
    }

    pub fn hex(&self) -> String {
        self.0.iter().map(|b| format!("{b:02x}")).collect()
    }
}

/// Temporary-file names unique within this process.
static SERIAL: AtomicU64 = AtomicU64::new(0);

/// The caches under one root directory.
#[derive(Debug, Clone)]
pub struct Cache {
    root: PathBuf,
    on: bool,
}

impl Cache {
    pub fn new(root: impl Into<PathBuf>) -> Cache {
        Cache {
            root: root.into(),
            on: true,
        }
    }

    /// A cache that holds nothing: every get misses, every put is dropped.
    pub fn off() -> Cache {
        Cache {
            root: PathBuf::new(),
            on: false,
        }
    }

    pub fn is_on(&self) -> bool {
        self.on
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Where `stage`'s entry for `key` lives. Refuses a stage name that is not
    /// one path component of `[A-Za-z0-9_.-]`.
    pub fn path(&self, stage: &str, key: &Key) -> io::Result<PathBuf> {
        let ok = !stage.is_empty()
            && stage != "."
            && stage != ".."
            && stage
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-'));
        if !ok {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("stage name {stage:?} is not one path component"),
            ));
        }
        let hex = key.hex();
        Ok(self
            .root
            .join(stage)
            .join(&hex[..2])
            .join(format!("{hex}.bin")))
    }

    /// The entry's bytes, or None when there is none.
    pub fn get(&self, stage: &str, key: &Key) -> io::Result<Option<Vec<u8>>> {
        if !self.on {
            return Ok(None);
        }
        match fs::read(self.path(stage, key)?) {
            Ok(b) => Ok(Some(b)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Store `bytes` as the entry: written to a temporary file beside it,
    /// flushed, then renamed over it.
    pub fn put(&self, stage: &str, key: &Key, bytes: &[u8]) -> io::Result<()> {
        if !self.on {
            return Ok(());
        }
        let path = self.path(stage, key)?;
        let dir = path.parent().expect("an entry path has a directory");
        fs::create_dir_all(dir)?;
        let tmp = dir.join(format!(
            "{}.{}.{}.tmp",
            key.hex(),
            std::process::id(),
            SERIAL.fetch_add(1, Ordering::Relaxed)
        ));
        let written = (|| {
            {
                // Closed before the rename (Windows renames no open file).
                let mut f = fs::File::create(&tmp)?;
                f.write_all(bytes)?;
                f.sync_all()?;
            }
            fs::rename(&tmp, &path)
        })();
        if written.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        written
    }

    /// The entry's bytes, computing and storing them on a miss.
    pub fn get_or_put<E: From<io::Error>>(
        &self,
        stage: &str,
        key: &Key,
        make: impl FnOnce() -> Result<Vec<u8>, E>,
    ) -> Result<Vec<u8>, E> {
        if let Some(b) = self.get(stage, key)? {
            return Ok(b);
        }
        let b = make()?;
        self.put(stage, key, &b)?;
        Ok(b)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> Cache {
        let dir = std::env::temp_dir().join(format!("ktpu-cache-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        Cache::new(dir)
    }

    #[test]
    fn key_is_sha256_of_length_prefixed_fields() {
        // hashlib.sha256(b"".join(len(p).to_bytes(8, "little") + p for p in
        //   (b"l0", b"1", b"\x01\x02", b"abc"))).hexdigest()
        let k = Key::of("l0", "1", &[1, 2], b"abc");
        assert_eq!(
            k.hex(),
            "cc03c5d30ac61e0d7a3fca6115785d8e3f028c62e3abbd62dbd87b509e5d1293"
        );
    }

    #[test]
    fn key_changes_with_every_field_and_split() {
        let k = Key::of("l1", "3", b"m", b"in");
        assert_ne!(k, Key::of("l2", "3", b"m", b"in"));
        assert_ne!(k, Key::of("l1", "4", b"m", b"in"));
        assert_ne!(k, Key::of("l1", "3", b"n", b"in"));
        assert_ne!(k, Key::of("l1", "3", b"m", b"im"));
        assert_ne!(Key::of("a", "bc", b"", b""), Key::of("ab", "c", b"", b""));
    }

    #[test]
    fn get_after_put_and_miss_before() {
        let c = scratch("roundtrip");
        let k = Key::of("l0", "1", b"m", b"x");
        assert_eq!(c.get("l0", &k).unwrap(), None);
        c.put("l0", &k, b"bytes").unwrap();
        assert_eq!(c.get("l0", &k).unwrap().as_deref(), Some(&b"bytes"[..]));
        c.put("l0", &k, b"other").unwrap();
        assert_eq!(c.get("l0", &k).unwrap().as_deref(), Some(&b"other"[..]));
        let _ = fs::remove_dir_all(c.root());
    }

    #[test]
    fn stages_live_in_their_own_directories() {
        let c = scratch("stages");
        let k = Key::of("s", "1", b"m", b"x");
        c.put("l1", &k, b"one").unwrap();
        assert_eq!(c.get("l2", &k).unwrap(), None);
        let p = c.path("l1", &k).unwrap();
        assert!(p.starts_with(c.root().join("l1").join(&k.hex()[..2])));
        let _ = fs::remove_dir_all(c.root().join("l1"));
        assert_eq!(c.get("l1", &k).unwrap(), None);
        let _ = fs::remove_dir_all(c.root());
    }

    #[test]
    fn write_leaves_no_temporary_file() {
        let c = scratch("atomic");
        let k = Key::of("l0", "1", b"m", b"y");
        c.put("l0", &k, &[7; 4096]).unwrap();
        let dir = c.path("l0", &k).unwrap().parent().unwrap().to_path_buf();
        let names: Vec<String> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(names, [format!("{}.bin", k.hex())]);
        let _ = fs::remove_dir_all(c.root());
    }

    #[test]
    fn off_holds_nothing() {
        let c = Cache::off();
        let k = Key::of("l0", "1", b"m", b"x");
        c.put("l0", &k, b"bytes").unwrap();
        assert_eq!(c.get("l0", &k).unwrap(), None);
        fn shared<T: Send + Sync>(_: &T) {}
        shared(&c);
    }

    #[test]
    fn stage_name_must_be_one_component() {
        let c = Cache::new("unused");
        let k = Key::of("", "", b"", b"");
        for bad in ["", "..", "a/b", "a\\b", "c:x"] {
            assert!(c.path(bad, &k).is_err(), "{bad}");
        }
    }

    #[test]
    fn get_or_put_computes_once() {
        let c = scratch("once");
        let k = Key::of("l0", "1", b"m", b"z");
        let mut calls = 0;
        let a: io::Result<Vec<u8>> = c.get_or_put("l0", &k, || {
            calls += 1;
            Ok(vec![1, 2])
        });
        let b: io::Result<Vec<u8>> = c.get_or_put("l0", &k, || panic!("cached"));
        assert_eq!((a.unwrap(), b.unwrap(), calls), (vec![1, 2], vec![1, 2], 1));
        let _ = fs::remove_dir_all(c.root());
    }
}
