//! The image room under the image scheduler (L1.SCHED.007, L2.SEARCH.001): a
//! ViT step's GELU gradient stream, whose fastest written image fits and whose
//! order at price 0 does not; and a stream that fits until its throttle syncs.

use ktpu_l1::sched::schedule::whole;
use ktpu_l1::text::Module;
use ktpu_l2::lower::vector::encode::{cycles, words_of};
use ktpu_l2::target::IMEM_ROOM;
use ktpu_l2::{compile_with, Program, Search, Target, PASSES};
use ktpu_machine::Machine;

const GELU_GRAD: &str = include_str!("fixtures/gelu_grad.l2");
const WIDE_MUL: &str = include_str!("fixtures/wide_mul.l2");

type Res = Result<(), String>;

fn target() -> Target {
    Target::of(&Machine::named("vlt_v9t6s_1n").unwrap()).with_nodes(1)
}

fn compiled(text: &str, passes: &[&str], schedule: bool) -> Result<Module, String> {
    let p = Program::parse(text, "<kernel>", None).map_err(|e| format!("read: {e}"))?;
    let got = compile_with(&p, &target(), passes, schedule, &Search::default());
    Ok(got.map_err(|e| format!("compile: {e}"))?.l1)
}

/// The module's one image within the room: its core cycles.
fn in_room(m: &Module) -> Result<i64, String> {
    let t = target();
    let img = m.images.values().next().ok_or("no image")?;
    let n = img.params.len();
    let words = words_of(m, &img.name, n, &t.machine).map_err(|e| format!("encode: {e}"))?;
    if words as i64 > IMEM_ROOM {
        return Err(format!("{words} words past the room"));
    }
    let vc = &t.machine.vc;
    let fill = (vc.fill_alone.0 as i64, vc.fill_alone.1 as i64);
    cycles(m, &img.name, t.vregs, t.vcopies, fill, &t.machine).map_err(|e| format!("time: {e}"))
}

/// The search takes a candidate the scheduler keeps at price 0.
fn the_search_takes_a_whole_image() -> Res {
    let t = target();
    let written = compiled(GELU_GRAD, &[], false)?;
    let kept = whole(&written, t.vcopies, &t.machine).map_err(|e| format!("whole: {e}"))?;
    let before = in_room(&written)?;
    let got = in_room(&compiled(GELU_GRAD, &[], true)?)?;
    match kept && got * 10 < before * 9 {
        true => Ok(()),
        false => Err(format!("whole {kept}, {got} cycles against {before}")),
    }
}

/// The wide stream, throttled, within the room written and scheduled.
fn the_throttled_stream_stands() -> Res {
    in_room(&compiled(WIDE_MUL, &[], false)?)?;
    in_room(&compiled(WIDE_MUL, &[], true)?).map(|_| ())
}

#[test]
fn a_scheduled_image_stays_within_the_room() -> Res {
    the_search_takes_a_whole_image()
}

#[test]
fn the_passes_keep_the_stream_within_the_room() -> Res {
    in_room(&compiled(GELU_GRAD, &PASSES, true)?).map(|_| ())
}

#[test]
fn a_throttled_image_stays_within_the_room() -> Res {
    the_throttled_stream_stands()
}

#[cfg(feature = "mutants")]
mod mutants {
    use super::*;
    use ktpu_l1::sched::schedule_module;
    use ktpu_l2::lower::fills::throttled;
    use ktpu_l2::mutant::{scoped, State};

    fn state(name: &'static str) -> State {
        State {
            active: Some(name),
            ..State::default()
        }
    }

    /// An image with no order that fits at price 0 fits at a price, faster
    /// than written.
    fn priced_into_the_room() -> Res {
        let t = target();
        let written = scoped(state("l2.vector/first-fit"), || {
            compiled(GELU_GRAD, &[], false)
        })?;
        if whole(&written, t.vcopies, &t.machine).map_err(|e| format!("whole: {e}"))? {
            return Err("the first fit is whole".into());
        }
        let before = in_room(&written)?;
        let tuned = schedule_module(&written, &t.machine).map_err(|e| format!("{e}"))?;
        let got = in_room(&tuned)?;
        match got * 10 < before * 9 {
            true => Ok(()),
            false => Err(format!("{got} cycles against {before}")),
        }
    }

    #[test]
    fn an_image_past_the_room_is_priced_into_it() -> Res {
        priced_into_the_room()?;
        let got = ktpu_l1::mutant::with("l1.sched/one-price", priced_into_the_room);
        assert!(got.is_err());
        Ok(())
    }

    #[test]
    fn a_scheduled_image_past_the_room_is_kept_out() {
        let got = ktpu_l1::mutant::with("l1.sched/unfit", the_search_takes_a_whole_image);
        assert!(got.is_err());
    }

    #[test]
    fn the_search_takes_the_first_fit_without_the_check() {
        let got = scoped(state("l2.vector/first-fit"), the_search_takes_a_whole_image);
        assert!(got.is_err());
    }

    /// The search's fastest image, no throttle in sight, stands; its syncs put
    /// it past the room.
    #[test]
    fn the_fastest_image_leaves_the_room_under_its_throttle() -> Res {
        let t = target();
        let plain = scoped(state("l2.fill/no-throttle"), || {
            compiled(WIDE_MUL, &[], false)
        })?;
        assert!(whole(&plain, t.vcopies, &t.machine).map_err(|e| format!("whole: {e}"))?);
        let before = in_room(&plain)?;
        let thrown = throttled(&plain, &t).map_err(|e| format!("throttle: {e}"))?;
        assert!(in_room(&thrown).is_err());
        let near = |m: &Module| Ok::<_, String>(in_room(m)? < 2 * before);
        assert!(near(&compiled(WIDE_MUL, &[], false)?)?);
        Ok(())
    }

    /// Past the room at its first initiation interval, the modulo schedule
    /// finds a later one; blind to the syncs it leaves the search to the band.
    #[test]
    fn the_modulo_schedule_fits_its_throttled_image() -> Res {
        let plain = scoped(state("l2.fill/no-throttle"), || {
            compiled(WIDE_MUL, &[], false)
        })?;
        let before = in_room(&plain)?;
        let band = scoped(state("l2.vector/modulo-unthrottled"), || {
            compiled(WIDE_MUL, &[], false)
        })?;
        assert!(in_room(&band)? > 2 * before);
        Ok(())
    }

    #[test]
    fn the_fit_sees_the_throttle_or_the_stream_leaves_the_room() {
        let got = scoped(
            state("l2.vector/fit-unthrottled"),
            the_throttled_stream_stands,
        );
        assert!(got.is_err());
    }
}
