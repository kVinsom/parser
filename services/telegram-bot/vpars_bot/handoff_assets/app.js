"use strict";
const $ = id => document.getElementById(id);
const screen = $("screen"), ctx = screen.getContext("2d"), tg = window.Telegram?.WebApp;
const path = location.pathname.replace(/\/$/, "");
let ws, seq = 0, frame = 0, pendingFrame = 0, down = false, lastMove = 0, lastSent = 0, stopped = false;
let pointer = null, dragMode = false, completed = false;
const states = {
  rejected:"Vinted повідомив про помилку створення оформлення. Оплату не натискали.",
  challenge:"Vinted показав сторінку захисту. Якщо доступна CAPTCHA, пройди її вручну тут. Якщо сайт повідомляє про блокування, заверши сеанс і звернися до підтримки Vinted.",
  login:"Vinted просить увійти. Віднови підключення цього регіону.",
  consent:"Зроби вибір щодо cookies у Vinted, потім продовж оформлення.",
  address_required:"Vinted не підставив адресу. Натисни «Продовжити оформлення», додай або вибери свою адресу та збережи її у Vinted. Її можна буде використати в наступних покупках.",
  address_editing:"Додай або вибери адресу у Vinted і натисни «Зберегти адресу» на його сторінці. Бот не заповнює адресу замість тебе.",
  checkout:"Перевір адресу, доставку та повну суму в оформленні Vinted. Кнопку оплати натискаєш ти.",
  vinted:"Вікно Vinted відкрите. Якщо треба, продовж оформлення.",
  unavailable:"Vinted зараз недоступний."
};
function status(text) { $("status").textContent = text; }
function result(text) {
  completed = stopped = true;
  status(text); screen.hidden = true; $("controls").hidden = true;
  $("placeholder").hidden = false; $("placeholder").textContent = text;
}
function send(value) {
  if (!ws || ws.readyState !== WebSocket.OPEN || !frame || stopped) return false;
  // Never buffer/replay an input across a broken connection.
  ws.send(JSON.stringify({...value, seq:++seq, frame})); lastSent = performance.now();
  return true;
}
async function drawFrame(blob, number) {
  let source, url;
  try {
    // Some WebViews lack ImageBitmap or cannot decode this blob with it.
    if (typeof createImageBitmap === "function") {
      try { source = await createImageBitmap(blob); } catch (_) { /* Use the image decoder below. */ }
    }
    if (!source) {
      url = URL.createObjectURL(new Blob([blob], {type:"image/jpeg"}));
      source = await new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img); img.onerror = reject; img.src = url;
      });
    }
    if (!stopped) {
      ctx.drawImage(source, 0, 0, screen.width, screen.height);
      frame = number; screen.hidden = false; $("placeholder").hidden = true; $("controls").hidden = false;
    }
  } finally {
    source?.close?.();
    if (url) URL.revokeObjectURL(url);
  }
}
function cancelPointer() {
  pointer = null;
  // Never manufacture mouseup: cancelling a drag must not submit a button.
  if (down) { down = false; stopped = true; ws?.close(); }
}
async function start() {
  try {
    if (!tg?.initData) throw Error("Відкрий це вікно кнопкою в особистому чаті бота у Telegram.");
    tg.ready(); tg.expand(); tg.enableClosingConfirmation?.(); tg.disableVerticalSwipes?.();
    const response = await fetch(path + "/auth", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({init_data:tg.initData})});
    const data = await response.json(); if (!response.ok) throw Error(data.error || "Доступ недоступний.");
    if (data.result) {result(data.text); return;}
    $("meta").textContent = `Vinted ${data.region} · збережений ліміт ${data.limit}`;
    const automatic = data.mode === "balance_first";
    $("payment-policy").textContent = automatic
      ? "Buy автоматично оплачує з балансу Vinted, якщо його вистачає на повну суму в межах твого ліміту. Якщо бракує — спосіб оплати вибираєш сам."
      : "Перед оплатою перевір адресу, доставку й повну суму. Оплату підтверджуєш сам.";
    status(automatic ? "Перевіряємо баланс і повну суму покупки…" : "Відкриваємо товар у твоїй сесії Vinted…");
    if (!/^\/purchase\/[0-9a-f]{32}\/stream$/.test(data.stream_path)) throw Error("Оформлення недоступне.");
    ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${data.stream_path}`);
    ws.binaryType = "blob";
    ws.onopen = () => ws.send(JSON.stringify({ticket:data.ticket}));
    // Ordered decode: an older frame must never overwrite a newer one.
    let render = Promise.resolve();
    ws.onmessage = event => {
      if (typeof event.data === "string") {
        const value = JSON.parse(event.data);
        if (value.type === "result") {result(value.text); return;}
        if (value.type === "note") {$("payment-notice").textContent = value.text; $("payment-notice").hidden = false;}
        if (value.type === "error") {status(value.text); stopped = true;}
        if (value.type === "frame") {pendingFrame = value.frame; status(states[value.state] || states.vinted);}
      } else {
        const number = pendingFrame;
        render = render.then(() => drawFrame(event.data, number)).catch(() => {
          stopped = true; ws?.close();
          status("Зображення недоступне. Відкрий вікно заново з бота.");
        });
      }
    };
    ws.onclose = () => {
      pointer = null; down = false; stopped = true; $("controls").hidden = true;
      screen.hidden = true; ctx.clearRect(0,0,screen.width,screen.height); $("placeholder").hidden = false;
      if (completed) return;
      $("placeholder").textContent = "З’єднання закрито. Відкрий це вікно знову з повідомлення бота. Натискання не повторюються.";
      if (!$("status").textContent.includes("недоступ")) status("З’єднання завершено. Перевір результат покупки у Vinted.");
    };
  } catch (error) {status(error.message || "Не вдалося відкрити вікно.");}
}
function point(event) {
  const r = screen.getBoundingClientRect();
  return {x:Math.max(0,Math.min(419,(event.clientX-r.left)*420/r.width)),y:Math.max(0,Math.min(819,(event.clientY-r.top)*820/r.height))};
}
screen.addEventListener("pointerdown", event => {
  if (pointer || event.button !== 0 || event.isPrimary === false || stopped || !frame) return;
  event.preventDefault(); screen.focus({preventScroll:true}); screen.setPointerCapture(event.pointerId);
  pointer = {id:event.pointerId, touch:event.pointerType === "touch" && !dragMode,
    x:event.clientX, y:event.clientY, lastY:event.clientY, scrolling:false};
  if (!pointer.touch) down = send({type:"down",...point(event)});
});
screen.addEventListener("pointermove", event => {
  if (!pointer || event.pointerId !== pointer.id) return;
  event.preventDefault();
  if (pointer.touch) {
    if (Math.hypot(event.clientX-pointer.x, event.clientY-pointer.y) > 8) pointer.scrolling = true;
    if (!pointer.scrolling || performance.now()-lastSent < 40) return;
    const delta = (pointer.lastY-event.clientY)*820/screen.getBoundingClientRect().height;
    pointer.lastY = event.clientY;
    send({type:"scroll",...point(event),delta:Math.max(-1500,Math.min(1500,delta))});
  } else if (down && performance.now()-lastMove >= 40 && performance.now()-lastSent >= 20) {
    lastMove=performance.now(); send({type:"move",...point(event)});
  }
});
screen.addEventListener("pointerup", event => {
  if (!pointer || event.pointerId !== pointer.id) return;
  event.preventDefault();
  const previous = pointer; pointer = null;
  if (previous.touch) {
    if (!previous.scrolling && Math.hypot(event.clientX-previous.x, event.clientY-previous.y) <= 8) {
      // A swipe never becomes a click, even if it began over Pay.
      if (send({type:"down",...point(event)})) send({type:"up",...point(event)});
    }
  } else if (down) {down=false;send({type:"up",...point(event)});}
});
screen.addEventListener("pointercancel", cancelPointer);
screen.addEventListener("lostpointercapture", cancelPointer);
screen.addEventListener("wheel", event => {
  event.preventDefault();
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 820 : 1;
  if(performance.now()-lastSent>40) send({type:"scroll",...point(event),delta:Math.max(-1500,Math.min(1500,event.deltaY*scale))});
},{passive:false});
screen.addEventListener("keydown", event => {if(event.ctrlKey||event.metaKey||event.altKey||event.repeat)return;event.preventDefault();if(event.key.length===1)send({type:"text",value:event.key});else if(["Backspace","Delete","Tab","Enter","Escape","ArrowLeft","ArrowRight","ArrowUp","ArrowDown"].includes(event.key))send({type:"key",value:event.key});});
$("typing").addEventListener("submit", event => {
  event.preventDefault();
  if ($("text").value && send({type:"text",value:$("text").value})) $("text").value="";
});
$("drag-mode").onclick=()=>{
  cancelPointer(); dragMode=!dragMode;
  $("drag-mode").setAttribute("aria-pressed",String(dragMode));
  $("drag-mode").textContent=dragMode ? "Перетягування: увімкнено" : "Перетягування: вимкнено";
};
document.querySelectorAll("[data-scroll]").forEach(button => button.onclick=()=>send({type:"scroll",delta:Number(button.dataset.scroll)}));
document.querySelectorAll("[data-key]").forEach(button => button.onclick=()=>send({type:"key",value:button.dataset.key}));
$("prepare").onclick=()=>send({type:"prepare"});
$("close").onclick=()=>{send({type:"close"});stopped=true;setTimeout(()=>{ws?.close();tg?.close();},200);};
window.addEventListener("pagehide",()=>{stopped=true;ws?.close();});
document.addEventListener("visibilitychange",()=>{if(document.hidden)cancelPointer();});
window.addEventListener("resize",cancelPointer);
start();

