"""The skin picker's listing contract: what is offered, in what order, from
which side of the shadowing rule — and what deliberately is NOT offered."""

from __future__ import annotations

from pathlib import Path

from bilisama.ui.skins import list_skin_packs, packaged_skins_root


def _pack(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "pet.json").write_text("{}", encoding="utf-8")


def test_builtin_tofu_leads_and_kirby_stays_off_the_shelf(tmp_path: Path) -> None:
    """The real packaged root: tofu first, kirby unlisted (internal preview,
    ledger #31 — `--skin kirby` still works, the picker just does not
    advertise it)."""
    cards = list_skin_packs(packaged_skins_root(), None)
    assert cards[0] == {"id": "tofu", "source": "builtin"}
    assert all(card["id"] != "kirby" for card in cards)


def test_user_packs_list_with_their_source(tmp_path: Path) -> None:
    user = tmp_path / "skins"
    _pack(user, "candy")
    _pack(user, "aurora")
    cards = list_skin_packs(packaged_skins_root(), user)
    ids = [card["id"] for card in cards]
    assert ids[0] == "tofu"
    assert ids[1:] == sorted(ids[1:]), "after tofu, name order"
    by_id = {card["id"]: card for card in cards}
    assert by_id["candy"]["source"] == "user"
    assert by_id["aurora"]["source"] == "user"


def test_a_user_pack_cannot_shadow_the_builtin_tofu(tmp_path: Path) -> None:
    """packagedOnly's listing half: a user directory named tofu exists, but
    the card keeps saying builtin — the page's mount pins the packaged copy
    for that name, so attributing it to the user would lie."""
    user = tmp_path / "skins"
    _pack(user, "tofu")
    cards = list_skin_packs(packaged_skins_root(), user)
    assert cards[0] == {"id": "tofu", "source": "builtin"}
    assert sum(card["id"] == "tofu" for card in cards) == 1


def test_a_user_pack_named_kirby_is_the_streamers_own_and_lists(tmp_path: Path) -> None:
    user = tmp_path / "skins"
    _pack(user, "kirby")
    cards = list_skin_packs(packaged_skins_root(), user)
    by_id = {card["id"]: card for card in cards}
    assert by_id["kirby"]["source"] == "user"


def test_directories_without_a_manifest_are_not_packs(tmp_path: Path) -> None:
    user = tmp_path / "skins"
    (user / "notes").mkdir(parents=True)  # no pet.json
    cards = list_skin_packs(packaged_skins_root(), user)
    assert all(card["id"] != "notes" for card in cards)


def test_a_missing_user_root_lists_the_packaged_side_only(tmp_path: Path) -> None:
    assert list_skin_packs(packaged_skins_root(), tmp_path / "absent") == list_skin_packs(
        packaged_skins_root(), None
    )
