(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SUIT = { m: "万", p: "饼", s: "条" };
  let state = null;
  let polling = false;
  let busy = false;
  let dismissedResult = "";
  let lastStateKey = "";
  let showAll = false;
  let debugInit = false;
  let chatCollapsed = true;
  let chatAutoOpened = false;

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

  function seatName(no) {
    const s = (state && state.seats || []).find((x) => x.no === no);
    return s ? s.name + (s.wind ? "·" + s.wind : "") : `${no}号`;
  }

  function llmLive(seat) {
    if (!seat || !seat.llm) return null;
    const stats = seat.llm;
    const wrap = document.createElement("span");
    wrap.className = "llm-live";
    const hitRate = Number(stats.hit_rate || 0);
    const recent = (Array.isArray(stats.recent_hit_rates) ? stats.recent_hit_rates : [])
      .slice(-5)
      .reverse();
    const recentText = recent
      .map((x) => (Number(x) * 100).toFixed(1) + "%")
      .join(" / ");
    const hit = document.createElement("span");
    hit.className = "hit";
    hit.title = "缓存命中率按与上一次请求体的复用计算"
      + (recentText ? `\n最近5次（最新在前）：${recentText}` : "");
    hit.textContent = `缓存 ${stats.prompt ? (hitRate * 100).toFixed(1) + "%" : "—"}`;
    const speed = document.createElement("span");
    speed.className = "speed";
    const rate = Number(stats.speed || 0);
    speed.textContent = `输出 ${rate ? rate.toFixed(1) + " tok/s" : "—"}`;
    wrap.append(hit, speed);
    return wrap;
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
    who.textContent = seat.name + (seat.wind ? " · " + seat.wind : "");
    head.appendChild(who);
    const live = llmLive(seat);
    if (live) head.appendChild(live);
    if (state.turn === seat.no && !state.result && !state.finished) {
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
      if (state.debug && showAll && seat.hand && seat.hand.length) {
        const hand = document.createElement("div");
        hand.className = "seat-hand";
        for (const t of seat.hand) hand.appendChild(makeTile(t, "mini"));
        panel.appendChild(hand);
      } else {
        const backs = document.createElement("div");
        backs.className = "backs";
        const n = Math.max(0, Number(seat.hand_count) || 0);
        for (let i = 0; i < n; i++) backs.appendChild(makeBack());
        panel.appendChild(backs);
      }
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

  function renderScorebar() {
    const round = $("round-text");
    const context = $("context-text");
    const scores = $("score-text");
    scores.replaceChildren();
    if (!state || !state.ok) {
      round.textContent = "第 — 局";
      context.textContent = "LLM上下文 —/—";
      return;
    }
    round.textContent = `第 ${state.round || 1} 局 · 第 ${state.lap || 1} 圈 · 第 ${state.steps || 0} 步`;
    context.textContent = `LLM上下文 ${state.context_round || 1}/${state.context_rounds || state.rounds || 1}`;
    if (state.context_cut_next) context.textContent += " · 下一局清空";
    else if (!state.finished && (state.context_round || 1) >= (state.context_rounds || state.rounds || 1)) {
      context.textContent += " · 本局后清空";
    }
    const board = state.board || {};
    for (let no = 1; no <= 4; no++) {
      const span = document.createElement("span");
      span.className = "score";
      const val = Number(board[String(no)] || 0);
      span.textContent = `${seatName(no)} `;
      const b = document.createElement("b");
      b.textContent = (val >= 0 ? "+" : "") + val;
      span.appendChild(b);
      scores.appendChild(span);
    }
  }

  function renderSpeechBar() {
    const bar = $("speechbar");
    if (!bar) return;
    const rows = (state && state.said) || [];
    bar.replaceChildren();
    bar.classList.toggle("hidden", !rows.length);
    for (const s of rows) {
      const item = document.createElement("div");
      item.className = "speech-item";
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = (s.name || `${s.seat}号`) + "：";
      const what = document.createElement("span");
      what.className = "what";
      what.textContent = s.text || "";
      item.append(who, what);
      bar.appendChild(item);
    }
  }

  function renderCenter() {
    const main = $("center-main");
    const sub = $("center-sub");
    const wall = $("center-wall");
    const last = $("center-last");
    const log = $("center-log");
    if (!state || !state.ok) {
      main.textContent = (state && state.message) || "等牌局";
      sub.textContent = "";
      wall.replaceChildren();
      last.replaceChildren();
      log.replaceChildren();
      return;
    }
    if (state.interrupted) {
      main.textContent = "牌桌出错";
      sub.textContent = `${state.error || "外部请求失败"} · 点“继续”重试`;
      wall.replaceChildren();
      last.replaceChildren();
      log.replaceChildren();
      return;
    }

    const phase = state.phase_label || "?";
    let title = "等牌局";
    let detail = `第 ${state.round || 1} 局 · 第 ${state.steps || 0} 步 · ${phase}`;
    if (state.rounds) {
      detail += ` · LLM上下文 ${state.context_round || 1}/${state.rounds}`;
    }
    if (state.finished) {
      title = state.round_info && state.round_info.ended_early ? "提前结束" :
        (state.result && state.result.type === "draw_game") ? "荒庄" : "本局结束";
    } else if (state.paused) {
      title = "牌局暂停";
      detail += " · 在下方聊天，点“继续”接着打";
    } else if (state.awaiting_next) {
      title = "本局结束，等下一局";
      detail += " · 点“下一局”继续";
    } else if (state.pending) {
      if (state.pending.seat === state.own_seat) {
        const kind = state.pending.kind;
        const hasBloom = (state.actions || []).some((a) => a.kind === "bloom");
        const hasTing = (state.actions || []).some((a) => a.kind === "ting");
        if (kind === "draw" && (hasBloom || hasTing)) {
          const choices = ["摸牌"];
          if (hasBloom) choices.push("后开花");
          if (hasTing) choices.push("报听");
          title = `杠后补张：选${choices.join("/")}`;
        }
        else if (kind === "draw") title = "轮到你摸牌";
        else if (kind === "discard") title = "轮到你了";
        else if (kind === "react") title = "别人打牌：选碰/吃/杠/胡/过";
        else if (kind === "ting_declare") title = "听牌了：报不报？";
        else title = "轮到你了";
      } else {
        title = `${state.pending.seat}号 等待出牌`;
        if (state.waiting && state.waiting.seat === state.pending.seat) {
          title += ` · 接入中 ${state.waiting.seconds || 0}s`;
        }
      }
    } else if (state.turn) {
      title = `等 ${state.turn}号`;
    }
    main.textContent = title;
    sub.textContent = detail;

    wall.replaceChildren();
    const wallText = document.createElement("span");
    let wtxt = `牌墙 <b>${state.wall_left || 0}</b> 张`;
    if (state.dice) wtxt += ` · 骰 ${state.dice[0]}+${state.dice[1]}=${state.dice[2]}`;
    wallText.innerHTML = wtxt;
    wall.appendChild(wallText);
    if (state.wall_last) wall.appendChild(makeTile(state.wall_last, "small"));

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
    if (state.log && state.log.length) rows.push(...state.log.slice(-3));
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

    const tingHint = $("ting-hint");
    if (tingHint) {
      const hints = state.ting_hints || [];
      if (hints.length) {
        tingHint.textContent = "听牌提醒：" + hints.map((h) => h.text).join("；");
        tingHint.classList.remove("hidden");
      } else {
        tingHint.textContent = "";
        tingHint.classList.add("hidden");
      }
    }

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

    const pauseBtn = $("btn-pause");
    const endBtn = $("btn-end");
    const undoBtn = $("btn-undo");
    const nextBtn = $("btn-next");
    const canNext = !!(state && (state.awaiting_next || (state.finished && state.result)));
    nextBtn.classList.toggle("hidden", !canNext);
    nextBtn.disabled = !canNext;
    if (!state || !state.ok) {
      pauseBtn.disabled = true;
      endBtn.disabled = true;
      undoBtn.disabled = true;
      pauseBtn.textContent = "暂停";
      endBtn.textContent = "提前结束上下文";
      hint.textContent = state && state.message ? state.message : "正在等牌桌…";
      return;
    }

    pauseBtn.textContent = state.paused ? "继续" : "暂停";
    pauseBtn.disabled = !(state.paused || (state.pending && state.pending.seat === state.own_seat));
    endBtn.textContent = state.context_cut_next ? "上下文已结束" : "提前结束上下文";
    endBtn.disabled = !state.awaiting_next || !!state.context_cut_next;
    undoBtn.disabled = !(state.running || state.finished);

    if (state.finished) {
      hint.textContent = state.result
        ? "本局结束，可以直接点“下一局”；只有“新开一桌”才重置。"
        : (state.round_info && state.round_info.ended_early
          ? "这一场提前结束了。"
          : "本局结束，可以看结算或再开一桌。");
      return;
    }
    if (state.paused) {
      hint.textContent = "牌局暂停中，可以聊天，点“继续”接着打。";
      return;
    }
    if (state.awaiting_next) {
      if (state.context_cut_next) {
        hint.textContent = "LLM 上下文已结束；现在聊天按轮次结束权限，可以追加或改长期笔记。记完点“下一局”清空。";
      } else if ((state.context_round || 1) >= (state.context_rounds || state.rounds || 1)) {
        hint.textContent = "这是 LLM 上下文窗口最后一局，下一局会清空；想改长期笔记点“提前结束上下文”后再聊。";
      } else {
        hint.textContent = "本局已结算；想改长期笔记点“提前结束上下文”后再聊。点“下一局”继续。";
      }
      return;
    }

    const mine = state.pending && state.pending.seat === state.own_seat;
    if (!mine) {
      if (state.pending) {
        const wait = state.waiting && state.waiting.seat === state.pending.seat
          ? `（接入中 ${state.waiting.seconds || 0}s）` : "";
        hint.textContent = `等 ${state.pending.seat}号 出手${wait}`;
      } else {
        hint.textContent = "等下一手";
      }
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

  function chatPhase() {
    if (!state) return "after";
    if (state.paused) return "pause";
    if (state.awaiting_next) return state.context_cut_next ? "after" : "between";
    return "after";
  }

  function renderChat() {
    const panel = $("chat-panel");
    const body = $("chat-body");
    const messages = $("chat-messages");
    const input = $("chat-input");
    const send = $("chat-send");
    const target = $("chat-target");
    const hint = $("chat-hint");
    if (!state || !state.ok) {
      messages.replaceChildren();
      panel.classList.add("hidden");
      input.disabled = true;
      send.disabled = true;
      if (target) target.disabled = true;
      hint.textContent = "等牌桌";
      return;
    }
    body.classList.toggle("hidden", chatCollapsed);
    $("btn-chat-toggle").textContent = chatCollapsed ? "展开" : "收起";

    messages.replaceChildren();
    const rows = state.chat || [];
    if (!rows.length) {
      const e = document.createElement("div");
      e.className = "chat-empty";
      e.textContent = state.can_chat ? "还没有聊天。暂停或一局结束时可以说话。" : "这桌没有能说话的座位。";
      messages.appendChild(e);
    } else {
      for (const m of rows) {
        const row = document.createElement("div");
        row.className = "chat-msg " + (m.role === "user" ? "user" : "buddy");
        const who = document.createElement("span");
        who.className = "who";
        who.textContent = m.role === "user" ? "你" : (m.name || state.talker || "牌友");
        row.appendChild(who);
        const text = document.createElement("div");
        text.textContent = m.text || "";
        row.appendChild(text);
        messages.appendChild(row);
      }
      messages.scrollTop = messages.scrollHeight;
    }

    const targets = state.chat_targets || [];
    if (target) {
      const wanted = target.value;
      target.replaceChildren();
      for (const t of targets) {
        const opt = document.createElement("option");
        opt.value = String(t.seat);
        opt.textContent = t.name || `${t.seat}号`;
        target.appendChild(opt);
      }
      if (Array.from(target.options).some((o) => o.value === wanted)) target.value = wanted;
      else if (targets.length) target.value = String(targets[0].seat);
    }

    const canChat = !!state.can_chat && targets.length > 0;
    const inChatMoment = !!(state.paused || state.awaiting_next || state.finished || state.result);
    panel.classList.toggle("hidden", !canChat || !inChatMoment);
    input.disabled = !canChat || !inChatMoment || busy;
    send.disabled = input.disabled || !target || !target.value;
    if (target) target.disabled = !canChat || !inChatMoment || busy;
    if (!canChat) hint.textContent = "这桌没有能说话的座位";
    else if (!inChatMoment) hint.textContent = "暂停或一局结束时可以聊";
    else if (state.context_cut_next) hint.textContent = "上下文已结束；现在可以追加或改长期笔记，聊完点“下一局”";
    else hint.textContent = state.paused ? "暂停中，选人聊，聊完点“继续”" : "选人聊；第一次发消息会带上本局播报";
    if ((state.paused || state.awaiting_next) && !chatAutoOpened) {
      chatCollapsed = false;
      chatAutoOpened = true;
    }
    if (!state.paused && !state.awaiting_next && !state.finished) {
      chatAutoOpened = false;
    }
    body.classList.toggle("hidden", chatCollapsed);
  }

  function resultKey(result, info) {
    if (!result && !info) return "";
    const r = result || {};
    return [info && info.round, info && info.steps, r.type, r.seat, r.tile, (r.hand || []).join(""),
            state.context_cut_next ? "cut" : ""]
      .join("|");
  }

  function showResult() {
    const result = state.result;
    const info = state.round_info;
    if (!result && !info) return;
    const key = resultKey(result, info);
    if (dismissedResult === key) return;
    $("modal-title").textContent = result && result.type === "draw_game"
      ? "荒庄"
      : info && info.ended_early ? "提前结束"
      : result && result.type === "win" ? `${result.seat || "?"}号 胡了`
      : "本局结束";
    const settle = (result && result.settle) || {};
    const how = result && (result.zimo ? "自摸" :
      result.rob ? `抢杠（${result.from || "?"}号）` :
      result.bloom ? "后开花" :
      result.from ? `${result.from}号点炮` : "");
    $("modal-text").textContent = settle.text ||
      (info && info.ended_early ? "这一场提前结束，当前局不结算。" :
        result && result.type === "draw_game" ? "牌墙摸空，本局荒庄。" : (how || "本局结束。"));

    $("modal-broadcast").textContent = (info && info.broadcast) || state.broadcast || "";
    $("modal-broadcast").style.display = $("modal-broadcast").textContent ? "" : "none";

    const fansHost = $("modal-fans");
    fansHost.replaceChildren();
    for (const f of settle.fans || []) {
      const fEl = document.createElement("span");
      fEl.className = "fan";
      fEl.textContent = f.label + (Number(f.fan) > 1 ? ` ×${f.fan}` : "");
      fansHost.appendChild(fEl);
    }
    $("modal-label").textContent = (settle.fans || []).length ? "番种" : "";

    const payersHost = $("modal-payers");
    payersHost.replaceChildren();
    const payers = settle.payers || {};
    const payerFans = settle.payer_fans || {};
    for (const [no, val] of Object.entries(payers)) {
      const tag = document.createElement("span");
      tag.className = "payer";
      const own = (payerFans[no] || []).map((x) => x.label).join("、");
      tag.textContent = `${seatName(Number(no))} 付 ${val} 分${own ? `（${own}）` : ""}`;
      payersHost.appendChild(tag);
    }
    if (!Object.keys(payers).length) {
      const empty = document.createElement("span");
      empty.className = "seat-empty";
      empty.textContent = "没有付款记录";
      payersHost.appendChild(empty);
    }

    const boardHost = $("modal-board");
    boardHost.replaceChildren();
    if (state.board) {
      const line = document.createElement("div");
      line.textContent = "累计：" + [1, 2, 3, 4].map((n) => {
        const v = Number(state.board[String(n)] || 0);
        return `${seatName(n)} ${v >= 0 ? "+" : ""}${v}`;
      }).join("　");
      boardHost.appendChild(line);
    }

    const handHost = $("modal-hand");
    handHost.replaceChildren();
    for (const t of (result && result.hand) || []) handHost.appendChild(makeTile(t, "small"));
    const meldHost = $("modal-melds");
    meldHost.replaceChildren();
    for (const m of (result && result.melds) || []) meldHost.appendChild(makeMeldGroup(m));
    const isWin = !!(result && result.type === "win");
    for (const id of ["modal-hand-label", "modal-hand", "modal-melds-label", "modal-melds"]) {
      $(id).style.display = isWin ? "" : "none";
    }

    const next = $("modal-next");
    const canNext = !!(state.awaiting_next || (state.finished && state.result));
    next.classList.toggle("hidden", !canNext);
    next.disabled = !canNext;
    const cut = $("modal-cut");
    cut.classList.toggle("hidden", !state.awaiting_next);
    cut.textContent = state.context_cut_next ? "上下文已结束" : "提前结束上下文";
    cut.disabled = !state.awaiting_next || !!state.context_cut_next;
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
    if (state.interrupted) {
      text.textContent = `牌桌出错${state.error ? " · " + state.error : ""} · 点“继续”重试`;
    } else if (state.finished) {
      text.textContent = state.result
        ? "本局结束 · 可继续下一局"
        : `这一场结束${state.error ? " · " + state.error : ""}`;
    } else if (state.paused) {
      text.textContent = "牌局暂停";
    } else if (state.running) {
      text.textContent = `牌桌运行中${state.steps != null ? " · 第 " + state.steps + " 步" : ""}`;
    } else if (state.error) {
      text.textContent = `牌桌停了：${state.error}`;
    } else {
      text.textContent = state.ok ? "牌桌已停" : "正在开桌…";
    }
    status.appendChild(text);

    const debugBtn = $("btn-debug");
    if (!state.debug) {
      showAll = false;
      debugInit = false;
    } else if (!debugInit) {
      showAll = true;
      debugInit = true;
    }
    debugBtn.disabled = !state.debug;
    debugBtn.classList.toggle("on", !!state.debug && showAll);
    debugBtn.textContent = state.debug ? `全牌：${showAll ? "开" : "关"}` : "全牌：关";
    debugBtn.title = state.debug ? "切换四家手牌可见" : "启动 webui.py 时加 --debug 才能查看";

    $("btn-resume").disabled = !!state.running && !state.finished;
    $("btn-resume").textContent = state.interrupted ? "继续" : "接着打";
    $("btn-continue").classList.toggle("hidden", !state.interrupted);
    $("btn-quit").disabled = !state.running && state.finished !== true;
    $("btn-settings").disabled = false;
    $("btn-saves").disabled = false;

    renderScorebar();
    renderSpeechBar();
    if (!state.ok) {
      renderCenter();
      $("my-hand").replaceChildren();
      $("my-melds").replaceChildren();
      renderActions();
      renderChat();
      return;
    }
    if (state.interrupted) {
      renderCenter();
      $("my-hand").replaceChildren();
      $("my-melds").replaceChildren();
      renderActions();
      renderChat();
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
    renderChat();
    if (state.result || state.round_info) showResult();
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
      renderChat();
    }
  }

  async function pollNow() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      state = await res.json();
      lastStateKey = "";
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

  function resetTableState() {
    dismissedResult = "";
    lastStateKey = "";
    chatAutoOpened = false;
    chatCollapsed = true;
    $("modal").classList.add("hidden");
  }

  function openSettings() {
    if (state && state.config) {
      $("set-players").value = state.config.players || "1=human,2=llm,3=random,4=random";
      $("set-dealer").value = state.config.dealer || "";
      $("set-base").value = state.config.base ?? 1;
      $("set-fan-cap").value = state.config.fan_cap ?? 8;
      $("set-seed").value = state.config.seed ?? "";
      $("set-steps").value = state.config.steps ?? 0;
      $("set-agent-wait").value = state.config.agent_wait ?? 0;
      $("set-offline").value = state.config.offline ? "1" : "0";
    }
    $("settings-modal").classList.remove("hidden");
  }

  function openContextSettings() {
    $("set-context-rounds").value = state && state.ok
      ? (state.context_rounds || state.rounds || 1)
      : 1;
    $("context-modal").classList.remove("hidden");
  }

  async function saveContextSettings() {
    const rounds = Number($("set-context-rounds").value || 1);
    const data = await post("/api/context-rounds", { rounds: rounds });
    if (data && data.ok) {
      $("context-modal").classList.add("hidden");
      lastStateKey = "";
      toast("LLM 上下文窗口已改为每 " + data.rounds + " 局清空。");
    }
  }

  async function startNewGame() {
    const body = {
      players: $("set-players").value.trim(),
      dealer: $("set-dealer").value.trim(),
      base: Number($("set-base").value || 1),
      fan_cap: Number($("set-fan-cap").value || 8),
      seed: $("set-seed").value.trim(),
      steps: Number($("set-steps").value || 0),
      agent_wait: Number($("set-agent-wait").value || 0),
      offline: $("set-offline").value === "1",
    };
    if (!body.players) body.players = "1=human,2=llm,3=random,4=random";
    $("settings-modal").classList.add("hidden");
    resetTableState();
    const data = await post("/api/new", body);
    if (data && data.ok) toast("新桌开好了，等牌局落盘。");
  }

  async function resumeGame() {
    const wasInterrupted = !!(state && state.interrupted);
    resetTableState();
    const data = await post("/api/resume", {});
    if (data && data.ok) toast(wasInterrupted ? "继续了。" : "接上存档了。");
    else if (data && data.message) toast(data.message);
  }

  async function quitGame() {
    const data = await post("/api/quit", {});
    if (data) toast("牌桌已停。");
  }

  async function togglePause() {
    if (!state) return;
    if (state.paused) {
      await post("/api/resume-play", {});
      return;
    }
    const data = await post("/api/pause", {});
    if (data && data.ok) toast("牌先停着，可以聊天。");
  }

  async function endGame() {
    if (!state || !state.awaiting_next) {
      toast("只能在局间（本局结束、下一局开始前）用。");
      return;
    }
    if (state.context_cut_next) {
      toast("当前上下文已结束；聊完点“下一局”清空。");
      return;
    }
    if (!confirm("结束当前 LLM 上下文窗口？会停在轮次结束状态，现在可以改长期笔记；点“下一局”时才清空对话。牌局、分数和座位都不动。")) return;
    const data = await post("/api/end", {});
    if (data && data.ok) {
      state.context_cut_next = true;
      lastStateKey = "";
      render();
      toast(data.message || "当前上下文已结束；点“下一局”时清空。");
    } else if (data && data.message) toast(data.message);
  }

  async function nextRound() {
    const data = await post("/api/next-round", {});
    if (data && data.ok) {
      $("modal").classList.add("hidden");
      dismissedResult = "";
      toast("下一局开始。");
    }
  }

  async function sendChat() {
    if (!state || !state.can_chat) return;
    const input = $("chat-input");
    const target = $("chat-target");
    const text = input.value.trim();
    const seat = target ? target.value : "";
    if (!text || !seat) return;
    input.value = "";
    const data = await post("/api/chat", { text: text, phase: chatPhase(), seat: Number(seat) });
    if (data && data.ok) {
      chatCollapsed = false;
      chatAutoOpened = true;
    }
  }

  async function openUndo() {
    $("undo-frames").replaceChildren();
    try {
      const res = await fetch("/api/snapshots", { cache: "no-store" });
      const snap = await res.json();
      if (!snap.frames || !snap.frames.length) {
        const e = document.createElement("div");
        e.className = "chat-empty";
        e.textContent = "这一场还没有快照。";
        $("undo-frames").appendChild(e);
      } else {
        for (const f of snap.frames.slice().reverse()) {
          const row = document.createElement("div");
          row.className = "frame-row";
          const label = document.createElement("span");
          label.textContent = `第 ${f.step} 步 · ${f.next || "?"} ${f.seat || "?"}号 · 牌墙 ${f.wall} 张`;
          row.appendChild(label);
          const btn = document.createElement("button");
          btn.textContent = "回到这手之前";
          btn.addEventListener("click", async () => {
            const r = await post("/api/rollback", { name: f.name });
            if (r && r.ok) {
              $("undo-modal").classList.add("hidden");
              resetTableState();
              toast("已回退，牌局从这一手之前接上。");
            }
          });
          row.appendChild(btn);
          $("undo-frames").appendChild(row);
        }
      }
    } catch (err) {
      toast("取快照失败：" + err);
    }
    $("undo-modal").classList.remove("hidden");
  }

  function scoreLine(board) {
    if (!board) return "";
    return Object.keys(board).sort((a, b) => Number(a) - Number(b))
      .map((s) => `${s}号 ${Number(board[s]) >= 0 ? "+" : ""}${board[s]}`)
      .join("，");
  }

  function resultLine(result) {
    if (!result) return "还没有结算";
    if (result.type === "draw_game") return "荒庄";
    if (!result.seat) return result.reason || result.type || "无结果";
    const tile = result.tile ? ` 胡 ${tileName(result.tile)}` : "";
    const how = result.zimo ? "自摸" : (result.from ? `${result.from}号点炮` : "");
    return `${result.seat}号${tile}${how ? `（${how}）` : ""}`;
  }

  async function openSaves() {
    const current = $("saves-current");
    const list = $("save-list");
    list.replaceChildren();
    current.textContent = state && state.ok
      ? `当前：第 ${state.round || 1} 局 · ${scoreLine(state.board)}`
      : "当前牌桌还没起来。";
    try {
      const res = await fetch("/api/saves", { cache: "no-store" });
      const data = await res.json();
      const saves = data.saves || [];
      if (!saves.length) {
        const empty = document.createElement("div");
        empty.className = "save-empty";
        empty.textContent = "还没有存档。点“存本场”把这一场冻起来。";
        list.appendChild(empty);
      } else {
        for (const item of saves) list.appendChild(saveRow(item));
      }
    } catch (err) {
      toast("取存档失败：" + err);
    }
    $("saves-modal").classList.remove("hidden");
  }

  function saveRow(item) {
    const row = document.createElement("div");
    row.className = "save-row";

    const head = document.createElement("div");
    head.className = "save-row-head";
    const left = document.createElement("div");
    const name = document.createElement("div");
    name.className = "save-name";
    name.textContent = item.name || item.id;
    left.appendChild(name);
    const meta = document.createElement("div");
    meta.className = "save-meta";
    const played = Number(item.rounds_played || 0);
    const expected = Number(item.rounds_expected || 0);
    const partial = item.history_partial ? ` · 明细 ${played}/${expected}（有缺局）` : "";
    meta.textContent = `${item.created_at || ""} · 第 ${item.round || "?"} 局${partial}`;
    left.appendChild(meta);
    head.appendChild(left);
    row.appendChild(head);

    const score = document.createElement("div");
    score.className = "save-score";
    score.textContent = scoreLine(item.board) || "累计分 —";
    row.appendChild(score);

    const last = document.createElement("div");
    last.className = "save-last";
    const settleText = item.last_result
      ? (item.last_result.settle_text || (item.last_result.settle && item.last_result.settle.text) || "")
      : "";
    last.textContent = `最后一局：${resultLine(item.last_result)}`
      + (settleText ? ` · ${settleText}` : "");
    row.appendChild(last);

    const actions = document.createElement("div");
    actions.className = "save-actions";
    const load = document.createElement("button");
    load.className = "gold";
    load.textContent = "接档";
    load.addEventListener("click", async () => {
      const warn = `接档「${item.name || item.id}」会覆盖当前牌桌。\n当前进度会先备份到 state.before-load-*.json。`;
      if (!window.confirm(warn)) return;
      const r = await post("/api/saves/load", { id: item.id });
      if (r && r.ok) {
        $("saves-modal").classList.add("hidden");
        resetTableState();
        toast(r.message || "已接档");
      }
    });
    actions.appendChild(load);
    const del = document.createElement("button");
    del.className = "ghost";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      if (!window.confirm(`删除存档「${item.name || item.id}」？`)) return;
      const r = await post("/api/saves/delete", { id: item.id });
      if (r && r.ok) {
        toast(r.message || "已删档");
        await openSaves();
      }
    });
    actions.appendChild(del);
    row.appendChild(actions);
    return row;
  }

  async function createSave() {
    const input = $("save-name");
    const name = input.value.trim();
    const r = await post("/api/saves", { name });
    if (r && r.ok) {
      input.value = "";
      toast(r.message || "已存档");
      await openSaves();
    }
  }

  function toggleDebug() {
    if (!state || !state.debug) return;
    showAll = !showAll;
    lastStateKey = "";
    render();
  }

  $("btn-debug").addEventListener("click", toggleDebug);
  $("btn-pause").addEventListener("click", togglePause);
  $("btn-end").addEventListener("click", endGame);
  $("btn-next").addEventListener("click", nextRound);
  $("btn-saves").addEventListener("click", openSaves);
  $("btn-undo").addEventListener("click", openUndo);
  $("btn-context").addEventListener("click", openContextSettings);
  $("btn-settings").addEventListener("click", openSettings);
  $("btn-resume").addEventListener("click", resumeGame);
  $("btn-continue").addEventListener("click", resumeGame);
  $("btn-quit").addEventListener("click", quitGame);
  $("btn-chat-toggle").addEventListener("click", () => {
    chatCollapsed = !chatCollapsed;
    lastStateKey = "";
    renderChat();
  });
  $("chat-send").addEventListener("click", sendChat);
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendChat();
  });
  $("settings-cancel").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
  $("settings-start").addEventListener("click", startNewGame);
  $("context-cancel").addEventListener("click", () => $("context-modal").classList.add("hidden"));
  $("context-save").addEventListener("click", saveContextSettings);
  $("undo-cancel").addEventListener("click", () => $("undo-modal").classList.add("hidden"));
  $("saves-close").addEventListener("click", () => $("saves-modal").classList.add("hidden"));
  $("save-create").addEventListener("click", createSave);
  $("save-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") createSave();
  });
  $("modal-next").addEventListener("click", nextRound);
  $("modal-cut").addEventListener("click", endGame);
  $("modal-close").addEventListener("click", () => $("modal").classList.add("hidden"));
  $("modal-new").addEventListener("click", () => {
    $("modal").classList.add("hidden");
    openSettings();
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("modal").classList.add("hidden");
      $("settings-modal").classList.add("hidden");
      $("context-modal").classList.add("hidden");
      $("undo-modal").classList.add("hidden");
      $("saves-modal").classList.add("hidden");
    }
  });

  poll();
})();
