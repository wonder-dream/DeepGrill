from app.difficulty import max_rounds_for, target_level_for


def test_target_level_for():
    assert target_level_for(1) == 2  # 入门 → 概念+原理
    assert target_level_for(2) == 3  # 基础 → 到权衡
    assert target_level_for(3) == 4  # 进阶 → 到边界
    assert target_level_for(4) == 5  # 深度 → 全链
    assert target_level_for(5) == 5  # 专家 → 全链


def test_max_rounds_for():
    assert max_rounds_for(1) == 6
    assert max_rounds_for(2) == 9
    assert max_rounds_for(3) == 12
    assert max_rounds_for(5) == 18
    assert max_rounds_for(5, config_max=10) == 10  # 全局上限封顶
    assert max_rounds_for(1, config_max=5) == 5
