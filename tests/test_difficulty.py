from app.difficulty import max_rounds_for, probe_tier_for, target_level_for


def test_target_level_for():
    assert target_level_for(1) == 2  # 入门 → 概念+原理
    assert target_level_for(2) == 3  # 基础 → 到权衡
    assert target_level_for(3) == 4  # 进阶 → 到边界
    assert target_level_for(4) == 5  # 深度 → 全链
    assert target_level_for(5) == 5  # 专家 → 全链


def test_probe_tier_for():
    assert probe_tier_for(1) == "light"
    assert probe_tier_for(2) == "light"
    assert probe_tier_for(3) == "medium"
    assert probe_tier_for(4) == "deep"
    assert probe_tier_for(5) == "deep"


def test_max_rounds_for():
    assert max_rounds_for(1) == 4  # 浅挖档收紧
    assert max_rounds_for(2) == 4
    assert max_rounds_for(3) == 8  # 中挖档
    assert max_rounds_for(4) == 12
    assert max_rounds_for(5) == 15
    assert max_rounds_for(5, config_max=10) == 10  # 全局上限封顶
    assert max_rounds_for(1, config_max=5) == 4
