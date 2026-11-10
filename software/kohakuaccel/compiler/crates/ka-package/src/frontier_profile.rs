//! Deterministic work accounting for the external frontier scaling witness.
use std::cell::Cell;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Work {
    /// Matrix entries visited, plus segmentation scans.
    pub entries: u64,
    pub snapshots: u64,
}

#[derive(Clone, Copy, Default)]
struct Profile {
    enabled: bool,
    work: Work,
}
thread_local! { static PROFILE: Cell<Profile> = Cell::new(Profile::default()); }

fn update(f: impl FnOnce(&mut Work)) {
    PROFILE.with(|p| {
        let mut v = p.get();
        if v.enabled {
            f(&mut v.work);
            p.set(v);
        }
    });
}
pub(crate) fn entries(n: usize) {
    update(|w| w.entries += n as u64);
}
pub(crate) fn snapshot() {
    update(|w| w.snapshots += 1);
}

/// Nesting and unwinding restore the caller's counters; threads are isolated.
pub fn measure<T>(f: impl FnOnce() -> T) -> (T, Work) {
    struct Restore(Profile);
    impl Drop for Restore {
        fn drop(&mut self) {
            PROFILE.with(|p| p.set(self.0));
        }
    }
    let old = PROFILE.with(|p| {
        p.replace(Profile {
            enabled: true,
            work: Work::default(),
        })
    });
    let restore = Restore(old);
    let result = f();
    let work = PROFILE.with(|p| p.get().work);
    drop(restore);
    (result, work)
}
