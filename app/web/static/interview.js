/* 面试页的 JS（ADR-0004：**只有这一页跑 JS**）。
 *
 * 它做两件事，都写在页面上、不藏状态：
 *   ① 录一段音，把它放进表单提交到 /interview/{id}/voice
 *   ② 语音 / 打字两种作答方式的切换
 *
 * 三条纪律（都是被 v1 教过的）：
 *
 * · **状态住服务端**（ADR-0004）。这里不缓存任何"当前第几轮""答了什么"——
 *   那些在库里，页面刷新之后由服务端重新渲染。这个文件里没有一处状态是为了
 *   "下次还用得上"而留的。
 *
 * · **没有监听器泄漏**（v1 的 104 个监听器：每个提交按钮各挂一个，页面重渲染
 *   就再加一层）。这里的做法是**脚本运行时一次性绑定**，而页面每次都是整页
 *   重渲染（表单 POST + 302），所以不存在"绑定累积"。若将来改成局部更新，
 *   必须换成**事件委托**——那是 v1 的结论（决策 34）。
 *
 * · **失败要说出来**。麦克风没权限、浏览器不支持 MediaRecorder、录音为空 ——
 *   三种都写进 #record-status 并自动切到打字；绝不静默地什么都不发生。
 */
(function () {
  "use strict";

  var voicePane = document.getElementById("voice-pane");
  var textPane = document.getElementById("text-pane");
  var recordBtn = document.getElementById("record-btn");
  var status = document.getElementById("record-status");
  var audioInput = document.getElementById("audio-input");
  var voiceForm = document.getElementById("voice-form");
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

  // 没有 getUserMedia / MediaRecorder 的环境（老浏览器、非 https 的局域网 IP）：
  // 直接说清楚并切到打字，而不是让"开始录音"按钮点了没反应。
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
    if (!parts || parts.length === 0) {
      say("什么都没录到 —— 再试一次，或者用打字作答。");
      return;
    }
    var blob = new Blob(parts, { type: recorder && recorder.mimeType ? recorder.mimeType : "audio/webm" });
    var file = new File([blob], "answer.webm", { type: blob.type });
    var transfer = new DataTransfer();
    transfer.items.add(file);
    audioInput.files = transfer.files;
    // 决策 33：**不设确认环节** —— 转写直接进判分，所以这里直接提交
    say("提交中…… 正在转写并判分。");
    recordBtn.disabled = true;
    voiceForm.submit();
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
