# 全局样式统一方案（2026-08-11）

## 背景
按钮/输入框/徽章/卡片样式不统一：圆角混用 3/4/6/8px、padding 2-9px、字号 11-15px、
文本对齐不一致、主页面与审核页各自定义相似规则。

## 设计 token（:root，style.css）
```
--radius-sm: 6px   按钮/输入框/下拉/徽章/标签 chip
--radius-md: 8px   卡片（card/question-card/rv-card/面板）
--radius-lg: 12px  弹窗（modal-card/rv-modal-card）
--btn-pad: 6px 14px          标准按钮
--btn-sm-pad: 4px 12px       小按钮（标签/徽章/工具类）
--input-pad: 6px 10px        输入框/下拉
--font-btn: 13px / --font-sm: 12px
```

## 统一目标（按组件族）
| 组件族 | padding | 圆角 | 字号 | 对齐 |
|---|---|---|---|---|
| 标准按钮（.primary/sidebar-daily/upload/logout/pager/modal-foot/rv-toolbar/rv-pager 等） | 6px 14px | 6px | 13px | 居中 |
| 小按钮（weak-tag/weak-tag-entry/admin-btn/fav-btn/日历按钮/rev-tags-open） | 4px 12px | 6px | 12px | 居中 |
| 侧边栏导航按钮 | 8px 12px | 6px | 14px | **保留左对齐**（用户确认） |
| 输入框/下拉（filter-bar/搜索/编辑区/表单/modal/rv-edit） | 6px 10px | 6px | 13px | — |
| 徽章 .badge | 1px 8px | 6px | 11px | 居中 |
| 卡片（.card/.question-card/.rv-card/面板） | — | 8px | — | — |
| 弹窗（.modal-card/.rv-modal-card） | — | 12px | — | — |
| Tab（.upload-tab/.auth-tabs） | 9px 16px | 6px | 13px | 居中（合并规则） |
| 工具栏（.rv-toolbar select 等） | 6px 10px | 6px | 13px | — |

## 改动文件
- `app/web/static/style.css`：新增 token；统一全部按钮/输入/徽章/卡片/弹窗/Tab 规则（约 30 处）
- `app/web/static/reviewer.css`：改用同 token（rv-* 组件与主页面一致）
- 无 HTML/JS 结构改动；动态生成类名（weak-tag 等）由统一规则覆盖
- 版本 bump（index.html ?v= 20260811v2 → v3）

## 验证
- node --check 无变化（纯 CSS）
- 浏览器目视检查：主界面各页 + 审核页按钮/输入/卡片/弹窗一致性
- 静态资源 200
