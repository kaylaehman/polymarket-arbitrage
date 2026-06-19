from utils.structural_bias import structural_score


def test_longshot_no_bias_positive():
    # NO at a longshot YES price is favored (repo#1 longshot bias)
    assert structural_score(price=0.10, side="NO", category="Sports") > 0


def test_yes_longshot_disfavored():
    assert structural_score(price=0.10, side="YES", category="Sports") <= 0


def test_category_edge_sports_gt_finance():
    assert structural_score(0.10, "NO", "Sports") > structural_score(0.10, "NO", "Finance")
