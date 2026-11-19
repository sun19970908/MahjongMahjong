(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SUIT = { m: "万", p: "饼", s: "条" };
  let state = null;
  let polling = false;
  let busy = false;
  let dismissedResult = "";
  let lastStateKey = "";

  function tileName(tid) {
    if (!tid) return "?";
    if (tid === "z5") return "红中";
    if (tid.length === 2 && SUIT[tid[0]] && /^[1-9]$/.test(tid[1])) {
      return tid[1] + SUIT[tid[0]];
    }
    return String(tid);
  }

  function validTile(tid) {
    return typeof tid === "string" && /^[mpsz][1-9]$/.test(tid);
  }

  function makeTile(tid, cls = "") {
    const el = document.createElement("div");
    el.className = "tile" + (cls ? " " + cls : "");
    el.title = tileName(tid);
    el.dataset.tile = tid || "";
    if (validTile(tid)) {
      el.style.backgroundImage = `url("/tiles/${tid}.png")`;
    }
    return el;
  }

  function makeBack() {
    const el = document.createElement("div");
    el.className = "back";
    return el;
  }

  function makeMeldGroup(meld) {
    const group = document.createElement("div");
    group.className = "meld-group";
    for (const t of meld.tiles || []) group.appendChild(makeTile(t, "mini"));
    return group;
  }

  function renderMelds(host, melds, emptyText) {
    host.replaceChildren();
    if (!melds || !melds.length) {
      const e = document.createElement("span");
      e.className = "seat-empty";
      e.textContent = emptyText || "副露 —";
      host.appendChild(e);
      return;
    }
    for (const m of melds) host.appendChild(makeMeldGroup(m));
  }

  function renderRiver(host, tiles, emptyText) {
    host.replaceChildren();
    if (!tiles || !tiles.length) {
      const e = document.createElement("span");
      e.className = "seat-empty";
      e.textContent = emptyText || "河 —";
      host.appendChild(e);
      return;
    }
    for (const t of tiles) host.appendChild(makeTile(t, "mini"));
  }

  function badge(text, cls = "") {
    const b = document.createElement("span");
    b.className = "badge" + (cls ? " " + cls : "");
    b.textContent = text;
    return b;
  }

  function seatSlot(no) {
    if (!state) return "bottom";
    const own = state.own_seat || 1;
    const delta = (no - own + 4) % 4;
    return ["bottom", "right", "top", "left"][delta] || "bottom";
  }

  function renderSeat(seat, slotName) {
    const host = $("slot-" + slotName);
    if (!host) return;
    const panel = document.createElement("div");
    panel.className = "seat-panel" + (seat.is_me ? " me" : "");

    const head = document.createElement("div");
    head.className = "seat-head";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = seat.name;
    head.appendChild(who);
    if (state.turn === seat.no && !state.result) {
      const turn = document.createElement("span");
      turn.className = "turn";
      turn.textContent = "● 当前";
      head.appendChild(turn);
    }
    const badges = document.createElement("span");
    badges.className = "badges";
    if (seat.dealer) badges.appendChild(badge("庄", "dealer"));
    if (seat.ting) badges.appendChild(badge("报听", "ting"));
    if (seat.piao) badges.appendChild(badge("飘", "piao"));
    if (seat.ting_closed) badges.appendChild(badge("闭门"));
    if (seat.kans) badges.appendChild(badge("杠×" + seat.kans));
    if (seat.dead) badges.appendChild(badge("已弃胡", "dead"));
    if (!seat.is_me) badges.appendChild(badge(`${seat.hand_count}张`));
    head.appendChild(badges);
    panel.appendChild(head);

    if (!seat.is_me) {
      const backs = document.createElement("div");
      backs.className = "backs";
      const n = Math.max(0, Number(seat.hand_count) || 0);
      for (let i = 0; i < n; i++) backs.appendChild(makeBack());
      panel.appendChild(backs);
    } else {
      const note = document.createElement("div");
      note.className = "seat-empty";
      note.textContent = `手牌 ${seat.hand_count} 张 · 看下方`;
      panel.appendChild(note);
    }

    const melds = document.createElement("div");
    melds.className = "melds";
    renderMelds(melds, seat.melds, "副露 —");
    panel.appendChild(melds);

    const river = document.createElement("div");
    river.className = "river";
    renderRiver(river, seat.discards, "河 —");
    panel.appendChild(river);

    host.replaceChildren(panel);
  }

  function renderCenter() {
    const main = $("center-main");
    const sub = $("center-sub");
    const wall = $("center-wall");
    const last = $("center-last");
    const log = $("center-log");
    const phase = state.phase_label || "?";
    let title = "等牌局";

    if (state.result) {
      title = state.result.type === "draw_game" ? "荒庄" : "本局结束";
    } else if (state.pending) {
      if (state.pending.seat === state.own_seat) {
        const kind = state.pending.kind;
        const hasBloom = (state.actions || []).some((a) => a.kind === "bloom");
        if (kind === "draw" && hasBloom) title = "杠后补张：选摸牌或后开花";
        else if (kind === "draw") title = "轮到你摸牌";
        else if (kind === "discard") title = "轮到你了";
        else if (kind === "react") title = "别人打牌：选碰/吃/杠/胡/过";
        else if (kind === "ting_declare") title = "听牌了：报不报？";
        else title = "轮到你了";
      } else {
        title = `${state.pending.seat}号 待出手`;
      }
    } else if (state.turn) {
      title = `等 ${state.turn}号`;
    }
    main.textContent = title;
    sub.textContent = `第 ${state.steps || 0} 步 · ${phase}`;

    wall.replaceChildren();
    const wallText = document.createElement("span");
    wallText.innerHTML = `牌墙 <b>${state.wall_left || 0}</b> 张`;
    wall.appendChild(wallText);
    if (state.wall_last) {
      wall.appendChild(makeTile(state.wall_last, "small"));
    }

    last.replaceChildren();
    if (state.last) {
      const t = document.createElement("span");
      t.textContent = `${state.last.seat}号 打`;
      last.appendChild(t);
      last.appendChild(makeTile(state.last.tile, "small"));
    } else if (state.dealer) {
      const t = document.createElement("span");
      t.textContent = `${state.dealer}号坐庄`;
      last.appendChild(t);
    }

    log.replaceChildren();
    const rows = [];
    if (state.log && state.log.length) rows.push(...state.log.slice(-2));
    if (state.said && state.said.length) {
      const s = state.said[state.said.length - 1];
      rows.push(`${s.name || s.seat + "号"}：${s.text}`);
    }
    for (const row of rows) {
      const line = document.createElement("div");
      line.textContent = row;
      log.appendChild(line);
    }
  }

  function renderMy() {
    if (!state.seats) return;
    const me = state.seats.find((s) => s.is_me);
    $("my-count").textContent = `${state.hand_count || 0} 张`;
    const tip = [];
    if (state.drawn) tip.push(`刚摸 ${tileName(state.drawn)}`);
    if (state.pending && state.pending.seat === state.own_seat && state.pending.kind === "discard") {
      tip.push("点手牌出牌");
    }
    $("my-tip").textContent = tip.join(" · ");

    renderMelds($("my-melds"), me ? me.melds : [], "副露 —");

    const hand = $("my-hand");
    hand.replaceChildren();
    const canDiscard = !!state.pending &&
      state.pending.seat === state.own_seat &&
      state.pending.kind === "discard" &&
      (state.actions || []).some((a) => a.kind === "discard");
    let drawnMarked = false;
    const tiles = state.hand || [];
    for (let i = tiles.length - 1; i >= 0; i--) {
      const tid = tiles[i];
      const el = makeTile(tid, "clickable");
      if (!drawnMarked && state.drawn && tid === state.drawn) {
        el.classList.add("drawn");
        drawnMarked = true;
      }
      if (canDiscard) {
        el.addEventListener("click", () => chooseTile(tid));
      } else {
        el.classList.remove("clickable");
        el.style.cursor = "default";
      }
      hand.appendChild(el);
    }
  }

  function renderActions() {
    const host = $("actions");
    host.replaceChildren();
    const hint = $("hint");
    hint.className = "hint";
    hint.textContent = "";

    if (!state || !state.ok) {
      hint.textContent = state && state.message ? state.message : "正在等牌桌…";
      return;
    }

    if (state.result) {
      hint.textContent = "本局结束，可以新开一桌。";
      return;
    }

    const mine = state.pending && state.pending.seat === state.own_seat;
    if (!mine) {
      hint.textContent = state.pending ? `等 ${state.pending.seat}号 出手` : "等下一手";
      return;
    }

    const actions = (state.actions || []).filter((a) => a.kind !== "discard");
    if (!actions.length) {
      if (state.pending.kind === "discard") {
        hint.textContent = "点上面的手牌出牌。";
        hint.classList.add("warn");
      } else {
        hint.textContent = "这一手没有可点的按钮。";
      }
      return;
    }

    for (const a of actions) {
      const btn = document.createElement("button");
      btn.className = a.kind === "win" || a.kind === "bloom" ? "gold" : "";
      btn.textContent = a.label || a.kind;
      btn.disabled = busy;
      btn.addEventListener("click", () => submitAction(a.index));
      host.appendChild(btn);
    }
    if (actions.some((a) => a.kind === "win")) {
      hint.textContent = "能胡就胡；按“过”会放弃本局胡牌资格。";
      hint.classList.add("warn");
    } else if (state.pending.kind === "discard") {
      hint.textContent = "点手牌出牌，或用上面的杠/胡/报听。";
    }
  }

  function resultKey(result) {
    if (!result) return "";
    return [result.type, result.seat, result.tile, (result.hand || []).join("")].join("|");
  }

  function showResult(result) {
    if (!result) return;
    const key = resultKey(result);
    if (dismissedResult === key) return;
    $("modal-title").textContent = result.type === "draw_game"
      ? "荒庄"
      : `${result.seat || "?"}号 胡了`;
    const settle = result.settle || {};
    const how = result.zimo ? "自摸" :
      result.rob ? `抢杠（${result.from || "?"}号）` :
      result.bloom ? "后开花" :
      result.from ? `${result.from}号点炮` : "";
    $("modal-text").textContent = settle.text ||
      (result.type === "draw_game" ? "牌墙摸空，本局荒庄。" : (how || "本局结束。"));

    const fansHost = $("modal-fans");
    fansHost.replaceChildren();
    for (const f of settle.fans || []) {
      const fEl = document.createElement("span");
      fEl.className = "fan";
      fEl.textContent = f.label + (Number(f.fan) > 1 ? ` ×${f.fan}` : "");
      fansHost.appendChild(fEl);
    }
    $("modal-label").textContent = (settle.fans || []).length ? "番种" : "";

    const handHost = $("modal-hand");
    handHost.replaceChildren();
    for (const t of result.hand || []) handHost.appendChild(makeTile(t, "small"));

    const meldHost = $("modal-melds");
    meldHost.replaceChildren();
    for (const m of result.melds || []) meldHost.appendChild(makeMeldGroup(m));

    const isWin = result.type === "win";
    for (const id of ["modal-hand-label", "modal-hand", "modal-melds-label", "modal-melds"]) {
      $(id).style.display = isWin ? "" : "none";
    }
    $("modal-label").textContent = (settle.fans || []).length ? "番种" : "";

    $("modal").classList.remove("hidden");
    dismissedResult = key;
  }

  function render() {
    if (!state) return;
    const key = JSON.stringify(state);
    if (key === lastStateKey) return;
    lastStateKey = key;
    const status = $("status");
    status.classList.toggle("stopped", !state.running);
    status.replaceChildren();
    const dot = document.createElement("span");
    dot.className = "dot";
    status.appendChild(dot);
    const text = document.createElement("span");
    if (state.result) {
      text.textContent = `本局结束${state.steps != null ? " · 第 " + state.steps + " 步" : ""}`;
    } else if (state.running) {
      text.textContent = `牌桌运行中${state.steps != null ? " · 第 " + state.steps + " 步" : ""}`;
    } else if (state.error) {
      text.textContent = `牌桌停了：${state.error}`;
    } else {
      text.textContent = state.ok ? "牌桌已停" : "正在开桌…";
    }
    status.appendChild(text);
    $("btn-resume").disabled = !!state.running || !!state.result;
    $("btn-quit").disabled = !state.running;

    if (!state.ok) {
      $("center-main").textContent = state.message || "正在开桌…";
      $("center-sub").textContent = "";
      $("center-wall").replaceChildren();
      $("center-last").replaceChildren();
      $("center-log").replaceChildren();
      $("my-hand").replaceChildren();
      $("my-melds").replaceChildren();
      $("actions").replaceChildren();
      $("hint").textContent = state.message || "";
      return;
    }

    const bySlot = {};
    for (const seat of state.seats || []) bySlot[seatSlot(seat.no)] = seat;
    for (const slot of ["top", "left", "right", "bottom"]) {
      const seat = bySlot[slot];
      if (seat) renderSeat(seat, slot);
      else $("slot-" + slot).replaceChildren();
    }
    renderCenter();
    renderMy();
    renderActions();
    if (state.result) showResult(state.result);
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      const data = await res.json();
      state = data;
      render();
    } catch (err) {
      state = { ok: false, running: false, message: "连不上网页牌桌：" + err };
      render();
    } finally {
      polling = false;
      window.setTimeout(poll, 650);
    }
  }

  function toast(text) {
    const el = $("toast");
    el.textContent = text;
    el.classList.add("show");
    window.clearTimeout(toast._t);
    toast._t = window.setTimeout(() => el.classList.remove("show"), 2600);
  }

  async function post(url, body) {
    if (busy) return null;
    busy = true;
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      const data = await res.json();
      if (!data.ok) toast(data.message || "操作没成功");
      await pollNow();
      return data;
    } catch (err) {
      toast("请求失败：" + err);
      return null;
    } finally {
      busy = false;
      renderActions();
    }
  }

  async function pollNow() {
    if (polling) return;
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      state = await res.json();
      render();
    } catch (err) {
      toast("刷新失败：" + err);
    }
  }

  function submitAction(index) {
    if (busy) return;
    post("/api/action", { index: index });
  }

  function chooseTile(tid) {
    if (!state || !state.actions) return;
    const found = state.actions.find((a) => a.kind === "discard" && a.tile === tid);
    if (!found) {
      toast("这张现在不能打。");
      return;
    }
    submitAction(found.index);
  }

  async function newGame() {
    dismissedResult = "";
    $("modal").classList.add("hidden");
    await post("/api/new", {});
    toast("新一桌开好了，等牌局落盘。");
  }

  async function resumeGame() {
    dismissedResult = "";
    $("modal").classList.add("hidden");
    await post("/api/resume", {});
  }

  async function quitGame() {
    await post("/api/quit", {});
    toast("牌桌已停。");
  }

  $("btn-new").addEventListener("click", newGame);
  $("btn-resume").addEventListener("click", resumeGame);
  $("btn-quit").addEventListener("click", quitGame);
  $("modal-new").addEventListener("click", newGame);
  $("modal-close").addEventListener("click", () => $("modal").classList.add("hidden"));

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") $("modal").classList.add("hidden");
  });

  poll();
})();
