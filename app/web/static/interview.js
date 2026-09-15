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

  function say(text) {
    status.textContent = text;
  }

  function mode() {
    var checked = document.querySelector('input[name="answer-mode"]:checked');
    return checked ? checked.value : "voice";
  }

  function applyMode() {
    var voice = mode() === "voice";
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
    // 流不可用（老浏览器 / fetch 被挡）：退回整页 POST —— 功能不丢，只是没有流式
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
      window.location.href = redirect;
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
  if (textForm) {
    textForm.addEventListener("submit", function (event) {
      if (!window.fetch || !window.TextDecoder) { return; } // 让浏览器正常提交
      event.preventDefault();
      submitStream(textForm);
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

  var recorder = null;
  var chunks = [];
  var recording = false;

  function stopTracks(stream) {
    // 不关掉轨道的话，浏览器的录音指示灯会一直亮着 —— 用户会以为还在录
    stream.getTracks().forEach(function (track) { track.stop(); });
  }

  function start() {
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.addEventListener("dataavailable", function (event) {
        if (event.data && event.data.size > 0) { chunks.push(event.data); }
      });
      recorder.addEventListener("stop", function () {
        stopTracks(stream);
        submit(chunks);
      });
      recorder.start();
      recording = true;
      recordBtn.textContent = "结束并提交";
      recordBtn.classList.add("recording");
      say("正在录音…… 说完点「结束并提交」。");
    }).catch(function (err) {
      // 权限被拒是最常见的：明确说"改用打字"，而不是留下一个死按钮
      say("拿不到麦克风（" + (err && err.name ? err.name : "未知原因") + "）—— 请用打字作答。");
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
    var type = recorder && recorder.mimeType ? recorder.mimeType : "audio/webm";
    var blob = new Blob(parts, { type: type });
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
      start();
      return;
    }
    // 第二次点击 = 结束。`stop` 事件里才提交（那里才拿得到最后一块数据）
    say("结束录音……");
    recorder.stop();
  });

  applyMode();
})();
