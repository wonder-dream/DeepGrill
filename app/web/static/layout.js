/* 布局交互：左侧边栏拖拽调宽 + 主框左右调宽句柄 + 主框滚动条的指针揭示。
 *
 * 思路同 deepseek-harness 的 AppFrame DragHandle（MIT）：
 * 独立命中条 + Pointer events + setPointerCapture，move 用 requestAnimationFrame
 * 节流，拖拽中给 body 挂 *-dragging 类关掉过渡（否则栏边缘追不上指针）。
 *
 * 宽度只活在内存：拖拽写入 --rail-drag / --content-w-drag（像素）覆盖对应变量，
 * 刷新即回默认 —— 同 harness 的"折叠即忘"取舍，也避开 ADR-0004"状态住服务端"
 * 的持久化问题。边界由 CSS 收口：收起态与窄屏不渲染句柄，这里的 clamp 只是第二道闸。
 */
(function () {
  var root = document.documentElement;
  var body = document.body;

  /* ---------------- 左侧边栏拖拽调宽 ---------------- */
  var railHandle = document.getElementById("rail-handle");
  var RAIL_MIN = 200;   // px，与 harness 的 SIDEBAR_MIN 同量级
  var RAIL_MAX = 480;

  /* 主框句柄要跟住 main 的左右边界；main 左边距随边栏宽度/收起态变、
   * 右边距随主框宽度变，所以凡改变这两者的时刻都要重新 sync（见各调用点）。
   * 声明提前：边栏拖拽的 apply 里也会调它。 */
  var mainEl = document.querySelector("main");
  var leftH = document.getElementById("main-handle-left");
  var rightH = document.getElementById("main-handle-right");

  function syncMainHandles() {
    if (!mainEl || !leftH || !rightH) return;
    var r = mainEl.getBoundingClientRect();
    leftH.style.left = (r.left - 28) + "px";   // 命中条 20px 宽，离线框边界 8px
    rightH.style.left = (r.right + 8) + "px";
    leftH.style.display = "block";
    rightH.style.display = "block";
  }

  if (railHandle) {
    railHandle.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      // 以按下瞬间的**实际渲染宽度**为基准（px），之后 基准 + dx ——
      // 避免夹紧后面板跳回存储值。
      var base = railHandle.parentElement.getBoundingClientRect().width;
      var origin = e.clientX;
      railHandle.setPointerCapture(e.pointerId);
      body.classList.add("rail-dragging");

      var latest = origin;
      var frame = null;
      var apply = function () {
        frame = null;
        var px = Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(base + latest - origin)));
        root.style.setProperty("--rail-drag", px + "px");
        syncMainHandles();   // 主框左边界在跟变
      };
      var onMove = function (ev) {
        latest = ev.clientX;
        if (!frame) frame = requestAnimationFrame(apply);
      };
      var onUp = function () {
        railHandle.removeEventListener("pointermove", onMove);
        railHandle.removeEventListener("pointerup", onUp);
        railHandle.removeEventListener("pointercancel", onUp);
        if (frame) {
          cancelAnimationFrame(frame);
          frame = null;
        }
        body.classList.remove("rail-dragging");
      };
      railHandle.addEventListener("pointermove", onMove);
      railHandle.addEventListener("pointerup", onUp);
      railHandle.addEventListener("pointercancel", onUp);
    });
  }

  /* ---------------- 主框左右调宽句柄 ----------------
   * 离线框边界 8px 的隐形命中条；悬停只在指针上下 80px 亮出琥珀渐隐段
   * （段的位置 = --seg-top，纯 CSS 定位不了"指针附近的一段"，必须是 JS）。
   * 主框居中，拖任一侧 = 两侧对称变：宽 基准 + 方向 * 2 * dx。 */
  var SEG_HALF = 80;
  var CONTENT_MIN = 560;
  var CONTENT_MAX = 1680;

  function segFollow(handle) {
    handle.addEventListener("mousemove", function (e) {
      handle.style.setProperty("--seg-top", (e.clientY - SEG_HALF) + "px");
      handle.classList.add("hot");
    });
    handle.addEventListener("mouseleave", function () {
      handle.classList.remove("hot");
    });
  }

  function dragContent(handle, dir) {
    segFollow(handle);
    handle.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || !mainEl) return;
      e.preventDefault();
      var base = mainEl.getBoundingClientRect().width;
      var origin = e.clientX;
      handle.setPointerCapture(e.pointerId);
      body.classList.add("main-dragging");

      var latest = origin;
      var frame = null;
      var apply = function () {
        frame = null;
        var w = Math.min(
          CONTENT_MAX,
          Math.max(CONTENT_MIN, Math.round(base + dir * 2 * (latest - origin)))
        );
        root.style.setProperty("--content-w-drag", w + "px");
        syncMainHandles();   // 右句柄要跟住新边界
      };
      var onMove = function (ev) {
        latest = ev.clientX;
        handle.style.setProperty("--seg-top", (ev.clientY - SEG_HALF) + "px");
        if (!frame) frame = requestAnimationFrame(apply);
      };
      var onUp = function () {
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
        if (frame) {
          cancelAnimationFrame(frame);
          frame = null;
        }
        body.classList.remove("main-dragging");
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
    });
  }

  if (mainEl && leftH && rightH) {
    dragContent(leftH, -1);   // 左句柄向左拖 = 加宽
    dragContent(rightH, 1);   // 右句柄向右拖 = 加宽
    syncMainHandles();
    window.addEventListener("resize", syncMainHandles);
    var toggle = document.getElementById("nav-toggle");
    if (toggle) {
      toggle.addEventListener("change", function () {
        setTimeout(syncMainHandles, 200);   // 等收起/展开的过渡结束再对齐
      });
    }
  }

  /* 页面（主框）滚动条：指针进入右缘 HOT_ZONE 像素内才点亮琥珀 thumb。
   * 纯 CSS 表达不了"指针在滚动条附近"（:hover 只认元素盒，而视口滚动条属于
   * html），这里用 deepseek-harness 同思路的指针跟踪，粒度从列简化成视口。
   * sidebar / modal 的滚动条仍由 CSS :hover 负责（app.css 滚动条一节）。 */
  var HOT_ZONE = 24;
  document.addEventListener("mousemove", function (e) {
    var hot = e.clientX >= window.innerWidth - HOT_ZONE;
    root.classList.toggle("scrollbar-hot", hot);
  });
})();
