//! Tier-0 replay ledger checks.
use ktpu_l2::Search;

#[test]
fn retile_ledger_is_immutable_when_frozen() {
    let search = Search::recording();
    search.remember_retile(0, 2, 8);
    search.remember_retile(1, 1, 16);
    search.freeze();
    search.remember_retile(0, 9, 1);
    assert_eq!(search.retile_choice(0), Some((2, 8)));
    assert_eq!(search.retile_choice(1), Some((1, 16)));
    assert_eq!(search.retile_choice(2), None);
    assert_eq!(search.retile_count(), 2);
    let mutant = Search::recording();
    mutant.remember_retile(0, 2, 8);
    mutant.remember_retile(0, 9, 1);
    assert_ne!(mutant.retile_choice(0), search.retile_choice(0));
}

#[test]
fn ordinary_search_does_not_record_template_decisions() {
    let search = Search::default();
    search.remember_retile(0, 2, 8);
    assert_eq!(search.retile_count(), 0);
    assert!(!search.frozen());
}
