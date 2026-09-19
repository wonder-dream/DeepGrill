/* 面试页的 JS（ADR-0004：**只有这一页跑 JS**）。
 *
 * 它做三件事，都写在页面上、不藏状态：
 *   ① 录一段音，把它交给 /voice/stream（或没有 fetch 时交给 /voice）
 *   ② 把面试官的话**边收边显示**（SSE：`event: prose` / `done` / `error`）
 *   ③ 语音 / 打字两种作答方式的切换
 *
 * 四条纪律（都是被 v1 教过的）：
 *
 * · **状态住服务端**（ADR-0004）。这里不缓存任何"当前第几轮""答了什么"——那些在
 *   库里，流结束时我们**跳到服务端渲染的地址**，页面显示的永远是库里的真相。
 *   流只负责"让文字早点出现"，它不改变任何状态。
 *
 * · **渐进增强**。两条表单的 `action` 指向整页渲染的端点；带 JS 时用
 *   `data-stream-action` 换成流式端点。脚本没加载、或 fetch/流不可用，页面照常能用。
 *
 * · **没有监听器泄漏**（v1 的 104 个监听器：每个提交按钮各挂一个，页面重渲染就再
 *   加一层）。这里每次加载只绑一次，而整页重渲染会把旧的 DOM 一起丢掉。若将来改成
 *   局部更新，必须换成**事件委托**（决策 34）。
 *
 * · **失败要说出来**。麦克风没权限、浏览器不支持、流中途断了、服务端 `error` ——
 *   四种都写进状态行并给出下一步；绝不静默地什么都不发生。
 */
(function () {
  "use strict";

  var voicePane = document.getElementById("voice-pane");
  var textPane = document.getElementById("text-pane");
  var recordBtn = document.getElementById("record-btn");
  var status = document.getElementById("record-status");
  var audioInput = document.getElementById("audio-input");
  var voiceForm = document.getElementById("voice-form");
  var liveCard = document.getElementById("live-card");
  var liveText = document.getElementById("live-text");
  var liveStatus = document.getElementById("live-status");
  var radios = document.querySelectorAll('input[name="answer-mode"]');

  if (!voicePane || !recordBtn || !status || !audioInput || !voiceForm) {
    return; // 不是面试页（脚本只在这一页加载）
  }

  // ---------------------------------------------------------------- 考场进出
  /* 考场模式：界面里没有导航，两个出口都在考场条上 ——「退出」只是离开这一页
     （改状态的是「放弃本场」，那是个 POST）。所以**不拦浏览器后退**：拦了它
     只会让"退出之后想回上一页"变成一件要按两次的事，而出口本来就不止一个。
     第一版压了一条历史记录当"第三条出路"的挡板，退出按钮加进来之后就撤掉了。 */

  // ---------------------------------------------------------------- 题干打字机
  /* 终端模式的开场：新题（还没有任何一轮对话）时题干逐字打出，像面试官正在打字。
     渐进增强 —— 无 JS 时题干本来就是完整渲染的；已有对话的页面是追问的延续，
     不打字直接显示。 */
  var stemEl = document.getElementById("term-stem");
  if (stemEl && stemEl.getAttribute("data-fresh") === "1") {
    var fullStem = stemEl.getAttribute("data-stem") || stemEl.textContent;
    var stemPos = 0;
    /* 减弱动效偏好要**真的**被尊重：CSS 那侧（app.css 的 prefers-reduced-motion）
       关掉了所有动画，但打字机是 JS 的 setTimeout —— 不在这里判一下，偏好就被绕过，
       而且一段 60 字的题干要"打"约 2.5 秒，等待是强加的。 */
    var calm = window.matchMedia
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (calm) {
      // 一轮都不打：直接给完整题干、也不挂光标（`.typing` 那个闪烁的块）
      stemEl.textContent = fullStem;
    } else {
      stemEl.textContent = "";
      stemEl.classList.add("typing");
      var typeStem = function () {
        stemPos += 1;
        stemEl.textContent = fullStem.slice(0, stemPos);
        if (stemPos < fullStem.length) {
          window.setTimeout(typeStem, 42);
        } else {
          stemEl.classList.remove("typing");
        }
      };
      window.setTimeout(typeStem, 400);
    }
  }

  function say(text) {
    status.textContent = text;
  }

  function mode() {
    var checked = document.querySelector('input[name="answer-mode"]:checked');
    return checked ? checked.value : "voice";
  }

  /* ---------------- 录音的四个状态 ----------------
   * 声明在 applyMode **之前**是刻意的：原来它们在文件下半部分（`var` 会提升，
   * 值仍是 undefined），于是"录音中切模式"的判断在初始化那一刻读到的是假值 ——
   * 那类 bug 只在特定点击顺序下出现，很难复现。
   *
   * `discarding` 的语义要精确：**只有真的停下了一台 recorder 才置位**，因为唯一
   * 清它的地方是 recorder 的 `stop` 事件。授权窗口期（`starting` 为真、`recorder`
   * 还是 null）里 `stop` 根本不会触发，无条件置位会让它永久停在 true —— 于是下一次
   * 录音的 `stop` 被当成"要丢弃的"直接 return（整段录音静默消失、状态行卡住）。 */
  var recorder = null;
  var chunks = [];
  var recording = false;
  var starting = false;
  var discarding = false;
  var mimeType = "";

  function applyMode() {
    var voice = mode() === "voice";
    // 录音中切去打字：停止并丢弃这段录音 —— 否则录音按钮随 voice-pane 隐藏，
    // 轨道一直开到页面跳转（浏览器录音指示灯常亮，且这段录音哪也到不了）
    if (!voice && (recording || starting)) { discardRecording(); }
    voicePane.hidden = !voice;
    textPane.hidden = voice;
  }

  for (var i = 0; i < radios.length; i++) {
    radios[i].addEventListener("change", applyMode);
  }

  // ---------------------------------------------------------------- 流式接收
  function showLive(text) {
    if (liveCard) { liveCard.hidden = false; }
    if (liveText) { liveText.textContent = text; }
  }

  function setLiveStatus(text) {
    if (liveStatus) { liveStatus.textContent = text; }
  }

  /* 读 SSE 帧。`fetch` 的 body 是字节流，所以要自己攒行 —— 一帧以空行结束，
     而一帧可能被切在任意位置（包括 "event: pro" 中间）。 */
  function readFrames(buffer, onFrame) {
    var parts = buffer.split("\n\n");
    var rest = parts.pop();
    for (var i = 0; i < parts.length; i++) {
      var event = "";
      var data = "";
      var lines = parts[i].split("\n");
      for (var j = 0; j < lines.length; j++) {
        if (lines[j].indexOf("event:") === 0) { event = lines[j].slice(6).trim(); }
        if (lines[j].indexOf("data:") === 0) { data += lines[j].slice(5).trim(); }
      }
      if (event) { onFrame(event, data); }
    }
    return rest;
  }

  /* 发一轮并把流读到底。`onDone` 决定跳去哪一页。 */
  function sendStream(url, body, onDone) {
    var prose = "";
    showLive("");
    setLiveStatus("面试官正在回应……");

    return fetch(url, { method: "POST", body: body, headers: { "Accept": "text/event-stream" } })
      .then(function (response) {
        if (response.status === 400 && response.headers.get("content-type") &&
            response.headers.get("content-type").indexOf("application/json") >= 0) {
          // 语音那条路的环境类失败（没接 STT、录音太大…）：JSON 里有一句人话
          return response.json().then(function (payload) {
            var err = new Error(payload.error || "这一轮没成");
            err.kind = payload.kind;
            throw err;
          });
        }
        if (!response.ok || !response.body) {
          throw new Error("服务端返回了 " + response.status);
        }

        var reader = response.body.getReader();
        var decoder = new TextDecoder("utf-8");
        var buffer = "";
        var finished = false;

        function pump() {
          return reader.read().then(function (result) {
            if (result.done) {
              if (!finished) {
                // 流断了却没收到 done —— 不能假装成功，让调用方去重新加载页面
                onDone(null, prose, new Error("连接断了（没等到 done）"));
              }
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            buffer = readFrames(buffer, function (event, data) {
              // 终态只认第一次：重复的 `done`（或 done 之后又来一帧 error）会让页面
              // 既跳转又被 reload，状态行还会互相覆盖。
              if (finished) { return; }
              var payload = {};
              try { payload = JSON.parse(data); } catch (e) { payload = {}; }
              if (event === "prose") {
                prose += payload.delta || "";
                showLive(prose);
              } else if (event === "done") {
                finished = true;
                onDone(payload.redirect, prose, null);
              } else if (event === "error") {
                finished = true;
                onDone(null, prose, new Error(payload.message || "服务端出错了"));
              }
            });
            return pump();
          });
        }
        return pump();
      });
  }

  function fallbackToPlainPost(form, message) {
    // 流不可用（老浏览器 / fetch 被挡）：退回整页 POST —— 功能不丢，只是没有流式。
    // ⚠️ `form.submit()` **绕过原生的 required 校验与 submit 事件**，所以这里自己
    // 先问一次浏览器（`reportValidity`），否则"没有 fetch 的浏览器"反而能提交空作答。
    if (form.reportValidity && !form.reportValidity()) { return; }
    if (message) { setLiveStatus(message); }
    form.submit();
  }

  function submitStream(form) {
    var url = form.getAttribute("data-stream-action");
    if (!url || !window.fetch || !window.TextDecoder) {
      fallbackToPlainPost(form, "这个浏览器不支持流式显示，改为整页提交。");
      return;
    }
    sendStream(url, new FormData(form), function (redirect, prose, error) {
      if (error) {
        // 降级不静默：说明白 + 重新加载看服务端的真实状态（那一轮可能已经落库）
        setLiveStatus("这一轮没能完成（" + error.message + "）—— 正在刷新页面看最新状态。");
        window.setTimeout(function () { window.location.reload(); }, 800);
        return;
      }
      setLiveStatus("这一轮结束，正在打开最新状态……");
      // 服务端的 done 帧总是带 redirect，但"没带"时也要留在原地而不是跳去 /undefined
      if (redirect) { window.location.href = redirect; }
    }).catch(function (error) {
      if (error && error.kind === "stt_unavailable") {
        // 没接 STT：回到语音面板并提示改用打字（不跳转，用户原地就能改）
        say(error.message + " —— 请用打字作答。");
        recordBtn.disabled = false;
        recordBtn.textContent = "开始录音";
        recordBtn.classList.remove("recording");
        if (liveCard) { liveCard.hidden = true; }
        return;
      }
      say("这一轮没能完成：" + error.message + " —— 正在刷新页面看最新状态。");
      window.setTimeout(function () { window.location.reload(); }, 800);
    });
  }

  // 打字那条路：拦下提交，改走流式
  var textForm = textPane ? textPane.querySelector("form") : null;
  var textArea = textForm ? textForm.querySelector("textarea") : null;
  if (textForm && textArea) {
    /* 空作答不许走：`preventDefault()` 会把浏览器的原生校验（`required`）一并
      拦掉，而纯空格/换行是能通过 `required` 的 —— 那样的"作答"会真的去调一次模型
      并扣额度点。两道都要（浏览器校验 + 非空白）。 */
    var answerReady = function () {
      if (!textArea.value.trim()) {
        say("先写一句再提交 —— 空作答不算一轮。");
        textArea.focus();
        return false;
      }
      return true;
    };
    textForm.addEventListener("submit", function (event) {
      if (!window.fetch || !window.TextDecoder) { return; } // 让浏览器正常提交
      event.preventDefault();
      if (!answerReady()) { return; }
      submitStream(textForm);
    });
    /* 作答框自己的承诺："Enter 提交，Shift+Enter 换行"（placeholder）。实现它 ——
       `requestSubmit()` 会走上面那个 submit 处理器（连带校验），不是绕过它。 */
    textArea.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) { return; }
      if (event.ctrlKey || event.metaKey || event.altKey) { return; }
      event.preventDefault();
      if (typeof textForm.requestSubmit === "function") {
        textForm.requestSubmit();
      } else {
        textForm.submit();
      }
    });
  }

  // ---------------------------------------------------------------- 录音
  var supported = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
                     window.MediaRecorder);

  if (!supported) {
    say("这个浏览器不能录音（需要 https 与 MediaRecorder）—— 请用打字作答。");
    recordBtn.disabled = true;
    document.querySelector('input[name="answer-mode"][value="text"]').checked = true;
    applyMode();
    return;
  }

  function stopTracks(stream) {
    // 不关掉轨道的话，浏览器的录音指示灯会一直亮着 —— 用户会以为还在录
    stream.getTracks().forEach(function (track) { track.stop(); });
  }

  /* 切换作答方式时丢弃进行中的录音：停轨、停 recorder，不进 submit。
     `discarding` **只在真有一台活动 recorder 时才置位** —— 它的唯一清位点是那个
     recorder 的 `stop` 事件，而无条件置位会让"授权窗口期"这一支把它永久留在 true
     （见文件上半部分那四个状态变量的注释）。 */
  function discardRecording() {
    starting = false;
    recording = false;
    var active = recorder;
    recorder = null;
    if (active && active.state !== "inactive") {
      discarding = true;
      active.stop();
    }
    recordBtn.textContent = "开始录音";
    recordBtn.classList.remove("recording");
    say("已丢弃这段录音 —— 打完字直接提交即可。");
  }

  function start() {
    starting = true;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      starting = false;
      /* ⚠️ 授权窗口期里用户可能已经切到打字（applyMode 会调 discardRecording，
         而那时 recorder 还是 null、什么也停不掉）。不在这里补一刀的话，这段流会
         照常开始录 —— 麦克风指示灯亮着、而界面已经是打字模式（实测的竞态）。 */
      if (mode() !== "voice") {
        stopTracks(stream);
        return;
      }
      chunks = [];
      recorder = new MediaRecorder(stream);
      // mimeType 在**这里**记下来：点击"结束并提交"时 recorder 已经置空（见下），
      // 那时再读它永远是 undefined —— Firefox 的 ogg / Safari 的 mp4 会被标成 webm。
      mimeType = recorder.mimeType || "audio/webm";
      recorder.addEventListener("dataavailable", function (event) {
        if (event.data && event.data.size > 0) { chunks.push(event.data); }
      });
      recorder.addEventListener("stop", function () {
        stopTracks(stream);
        if (discarding) { discarding = false; return; }   // 被 discardRecording 停的，不提交
        submit(chunks);
      });
      recorder.start();
      recording = true;
      recordBtn.textContent = "结束并提交";
      recordBtn.classList.add("recording");
      say("正在录音…… 说完点「结束并提交」。");
    }).catch(function (err) {
      starting = false;
      // 权限被拒是最常见的：明确说"改用打字"，而不是留下一个死按钮。
      // 浏览器给的错误名（NotAllowedError…）翻成人话——技术名对用户没有意义。
      var why = {
        NotAllowedError: "没有拿到麦克风权限",
        NotFoundError: "没找到麦克风",
        NotReadableError: "麦克风被别的程序占着",
        SecurityError: "这个页面不允许录音（需要 https）"
      }[err && err.name] || "拿不到麦克风";
      say(why + " —— 请用打字作答。");
      document.querySelector('input[name="answer-mode"][value="text"]').checked = true;
      applyMode();
    });
  }

  function submit(parts) {
    recording = false;
    recordBtn.textContent = "开始录音";
    recordBtn.classList.remove("recording");
    if (!parts || parts.length === 0) {
      say("什么都没录到 —— 再试一次，或者用打字作答。");
      return;
    }
    var blob = new Blob(parts, { type: mimeType });
    var file = new File([blob], "answer.webm", { type: blob.type });
    var transfer = new DataTransfer();
    transfer.items.add(file);
    audioInput.files = transfer.files;

    // 决策 33：**不设确认环节** —— 转写直接进判分
    var url = voiceForm.getAttribute("data-stream-action");
    if (!url || !window.fetch || !window.TextDecoder) {
      say("提交中……（整页模式）");
      recordBtn.disabled = true;
      voiceForm.submit();
      return;
    }
    say("提交中…… 正在转写并判分。");
    recordBtn.disabled = true;
    submitStream(voiceForm);
  }

  recordBtn.addEventListener("click", function () {
    if (!recording) {
      if (starting) { return; }   // 授权窗口期内的重复点击
      start();
      return;
    }
    // 第二次点击 = 结束。`stop` 事件里才提交（那里才拿得到最后一块数据）。
    // recorder 在这时置空，stop 事件迟到/重复也伤不到 —— submit 只读 mimeType。
    var active = recorder;
    recorder = null;
    recording = false;
    say("结束录音……");
    if (active && active.state !== "inactive") { active.stop(); }
  });

  applyMode();
})();
