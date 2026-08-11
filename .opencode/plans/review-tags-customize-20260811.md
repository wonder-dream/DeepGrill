# 复习页主题自定义展示方案（2026-08-11）

## 背景
复习页「全部主题」区渲染全部 114 个词表标签，太多太杂。
需求：默认只显示当前岗位（含语言）相关主题，其余隐藏；加号弹窗让用户完全自定义展示集合（可隐藏默认主题）。

## 方案
### 可见性（纯前端，localStorage key: `review_visible_tags`）
- 无存储（首次）：岗位默认集 = focus 相关分类标签 ∪ 通用分类标签
  - backend+lang：我的语言分类 ∪ 语言无关 backend 分类 ∪ 通用
  - backend 无 lang：全部 backend 分类 ∪ 通用
  - 其他岗位：roles 含 focus 的分类 ∪ 通用
  - focus 未设置：全部
- 用户弹窗保存后：以存储的完整可见集为准（岗位变化不再影响）
- 「重置为岗位默认」：删除存储回到默认集

### 弹窗（复习页「全部主题」标题行 + 按钮）
- 按 16 分类分组列出全部主题，checkbox（勾选 = 显示）
- 保存 = 写完整可见集 → 重渲染；关闭不保存丢弃
- 重置默认按钮

### 渲染
- 「需要加强的」区（薄弱点 count>0）：全显示，不受可见集影响
- 「全部主题」区：visibleSet 过滤；可见集为空显示引导文案

## 改动文件
- app/web/static/app.js：loadReviewHome 改造 + visibleSet 逻辑 + 弹窗交互
- app/web/static/index.html：弹窗 HTML（view-review 区）
- app/web/static/style.css：弹窗样式
- 版本号 bump（index.html ?v=）

## 验证
- 浏览器：无 focus 全显示 → 设 focus 后默认集变化 → + 自定义保存/重置 → 薄弱点区不受影响
- node --check 语法 + 服务已运行
