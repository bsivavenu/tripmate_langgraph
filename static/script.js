(() => {
  const API_URL = "/api/plan";
  const THREAD_KEY = "tripmate_thread_id";

  const chat = document.getElementById("chat");
  const welcome = document.getElementById("welcome");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const newChatBtn = document.getElementById("new-chat");
  const startDateInput = document.getElementById("start-date");
  const reviewInput = document.getElementById("review");
  // remember the "review the draft" choice between visits
  try { if (localStorage.getItem("tripmate_review") === "0") reviewInput.checked = false; } catch (_) {}
  reviewInput.addEventListener("change", () => {
    try { localStorage.setItem("tripmate_review", reviewInput.checked ? "1" : "0"); } catch (_) {}
  });
  // YYYY-MM-DD in local time, n days from today
  const isoDate = (n = 0) => {
    const d = new Date();
    d.setDate(d.getDate() + n);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  startDateInput.min = isoDate(0);      // no past dates
  startDateInput.value = isoDate(60);   // pre-filled: same ~8 weeks default the backend uses

  // Mirrors the graph in backend.py: flight -> hotel -> weather -> itinerary -> final.
  const STEPS = [
    "Searching live flights",
    "Finding hotels",
    "Checking the weather",
    "Building your itinerary",
    "Writing the final plan",
  ];

  let threadId = null;
  try { threadId = localStorage.getItem(THREAD_KEY); } catch (_) {}

  function scrollDown() {
    chat.scrollTo({ top: chat.scrollHeight, behavior: "smooth" });
  }

  function addMessage(kind, content, { markdown = false } = {}) {
    if (welcome) welcome.remove();
    const row = document.createElement("div");
    row.className = `msg ${kind}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";

    if (markdown && window.marked && window.DOMPurify) {
      bubble.classList.add("md");
      bubble.innerHTML = DOMPurify.sanitize(marked.parse(content));
      bubble.querySelectorAll("a").forEach((a) => {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      });
    } else {
      bubble.textContent = content; // plain text: never inject unsanitised HTML
    }

    if (kind === "bot") {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-btn";
      copy.textContent = "Copy";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(content);
          copy.textContent = "Copied!";
        } catch (_) {
          copy.textContent = "Failed";
        }
        setTimeout(() => (copy.textContent = "Copy"), 1500);
      });
      bubble.appendChild(copy);
    }

    row.appendChild(bubble);
    chat.appendChild(row);
    scrollDown();
    return row;
  }

  // The backend is a single blocking call, so progress is an estimate that
  // advances on a timer and stops on the last step until the reply arrives.
  function addProgress() {
    if (welcome) welcome.remove();
    const row = document.createElement("div");
    row.className = "msg bot";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const title = document.createElement("p");
    title.className = "progress-title";
    title.textContent = "Your AI agents are planning your trip…";
    bubble.appendChild(title);
    const list = document.createElement("ul");
    list.className = "progress";
    const items = STEPS.map((label) => {
      const li = document.createElement("li");
      li.textContent = label;
      list.appendChild(li);
      return li;
    });
    bubble.appendChild(list);
    row.appendChild(bubble);
    chat.appendChild(row);

    let i = 0;
    items[0].classList.add("active");
    const timer = setInterval(() => {
      if (i >= items.length - 1) return;
      items[i].classList.replace("active", "done");
      i += 1;
      items[i].classList.add("active");
    }, 6000);

    scrollDown();
    return { row, stop: () => { clearInterval(timer); row.remove(); } };
  }

  function setBusy(busy) {
    sendBtn.disabled = busy;
    input.disabled = busy;
    if (!busy) input.focus();
  }

  async function submitQuery(query) {
    addMessage("user", query);
    setBusy(true);
    const progress = addProgress();

    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          thread_id: threadId,
          start_date: startDateInput.value || null,
          review: reviewInput.checked,
        }),
      });
      if (!res.ok) {
        let detail = `Request failed (${res.status})`;
        try {
          const body = await res.json();
          if (body.detail) detail = typeof body.detail === "string" ? body.detail : detail;
        } catch (_) {}
        throw new Error(detail);
      }
      const data = await res.json();
      if (data.thread_id) {
        threadId = data.thread_id;
        try { localStorage.setItem(THREAD_KEY, threadId); } catch (_) {}
      }
      progress.stop();
      // addMessage("bot", data.answer || "No answer was returned.", { markdown: true });
      handleResponse(data);

    } catch (err) {
      progress.stop();
      addMessage("error", `Something went wrong: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }
    // Either show the draft for approval, or show the final answer.
  function handleResponse(data) {
    if (data.needs_approval) {
      showApproval(data);
    } else {
      addMessage("bot", data.answer || "No answer was returned.", { markdown: true });
    }
  }

  // Draft itinerary + Approve / Request changes buttons.
  function showApproval(data) {
    const draft =
      `**Draft itinerary: please review**\n\n${data.draft_itinerary}\n\n` +
      (data.budget_table ? `**Estimated budget**\n\n${data.budget_table}` : "");
    const row = addMessage("bot", draft, { markdown: true });

    const box = document.createElement("div");
    box.className = "approval";
    const feedback = document.createElement("textarea");
    feedback.placeholder = "Want changes? Describe them here (e.g. add a museum on day 2)";
    feedback.rows = 2;
    const approveBtn = document.createElement("button");
    approveBtn.type = "button";
    approveBtn.className = "approve-btn";
    approveBtn.textContent = "Approve";
    const reviseBtn = document.createElement("button");
    reviseBtn.type = "button";
    reviseBtn.className = "revise-btn";
    reviseBtn.textContent = "Request changes";
    box.append(feedback, approveBtn, reviseBtn);
    row.querySelector(".bubble").appendChild(box);

    approveBtn.addEventListener("click", () => {
      box.remove();
      sendDecision(true, "");
    });
    reviseBtn.addEventListener("click", () => {
      if (!feedback.value.trim()) { feedback.focus(); return; }  // need text to revise
      box.remove();
      sendDecision(false, feedback.value.trim());
    });
  }

  // Send the user's answer to /api/approve and show whatever comes back
  // (the final answer, or a new draft if they asked for changes).
  async function sendDecision(approved, feedback) {
    if (sendBtn.disabled) return;  // a request is already running: ignore repeat clicks
    setBusy(true);
    const progress = addProgress();
    try {
      const res = await fetch("/api/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thread_id: threadId, approved, feedback }),
      });
      if (!res.ok) {
        let detail = `Request failed (${res.status})`;
        try {
          const body = await res.json();
          if (body.detail && typeof body.detail === "string") detail = body.detail;
        } catch (_) {}
        throw new Error(detail);
      }
      const data = await res.json();
      progress.stop();
      handleResponse(data);
    } catch (err) {
      progress.stop();
      addMessage("error", `Something went wrong: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  function autoGrow() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = input.value.trim();
    if (!query || sendBtn.disabled) return;
    input.value = "";
    autoGrow();
    submitQuery(query);
  });

  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      input.value = chip.textContent;
      form.requestSubmit();
    });
  });

  // After a page reload, show again the draft that is still waiting for approval on the saved trip.
  async function restorePending() {
    if (!threadId) return;
    try {
      const res = await fetch(`/api/pending?thread_id=${encodeURIComponent(threadId)}`);
      if (!res.ok) return;
      const data = await res.json();
      if (data.pending && data.needs_approval) {
        if (data.query) addMessage("user", data.query);
        showApproval(data);
      }
    } catch (_) {}
  }
  restorePending();

  newChatBtn.addEventListener("click", () => {
    try { localStorage.removeItem(THREAD_KEY); } catch (_) {}
    location.reload();
  });
})();
