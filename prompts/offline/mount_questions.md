你要把一批**面试题**挂到**已有的知识点**上。

## 已有知识点（`point_id` 只能用这里的）

{{catalog}}

## 待挂载的题

{{questions}}

## 输出格式

只输出一个 JSON 对象，不要解释、不要 markdown 围栏：

{
  "assignments": [
    {"question_id": 1, "point_id": 7, "related_point_ids": [3], "confidence": "high"},
    {"question_id": 2, "point_id": null, "related_point_ids": [], "confidence": "low"}
  ]
}

## 规则（严格照做）

1. **每题给出一个 `point_id`（主知识点），且只能从上面的清单里选。**
   另可给 `related_point_ids`（**0 到 3 个**）：只有这道题**真的同时考到**另一个
   知识点时才加，宁少不多 —— 关联点会带进它的考察点，判分时一起判定命中。
   拿不准就留空数组：多挂一个点会让那个点的掌握度凭空多一格。
2. **挂不上就填 `null`** —— 宁可空着，也不要挂到一个勉强相关的地方。
   挂错会让掌握度矩阵被污染，而**错挂是看不出来的**。
   什么算挂不上：这道题考的东西在清单里没有对应；或者对应得很勉强。
3. `confidence` 填 `high` / `medium` / `low` —— 它会被人优先复查。
4. **不要新建知识点**。清单里没有就是没有；那属于人审决定的事。
5. 每道题都必须出现在 `assignments` 里（挂不上也要列出 `point_id: null`）。
