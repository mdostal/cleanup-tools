/*
 * Chat page (chat.html): creates a conversation on load, then for each
 * sent message starts a background job (POST /chat/<id>/message) and polls
 * its status (GET /chat/status/<job_id>) the same way plan-trigger.js polls
 * a plan job -- this is "streaming" without Server-Sent Events, per the
 * chat-agent-plan-builder design discussion §2.5: the job's `partial` field
 * grows as the model's response streams in server-side, and this file just
 * renders whatever the latest poll returned into the assistant's message
 * bubble, growing it in place.
 *
 * Deliberately vanilla JS with no build step or dependency, consistent
 * with the rest of this UI's static/*.js files.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 400;

  function init() {
    var transcript = document.getElementById("chat-transcript");
    var form = document.getElementById("chat-form");
    if (!transcript || !form) {
      return;
    }
    var input = document.getElementById("chat-input");
    var sendButton = document.getElementById("chat-send");
    var statusEl = document.getElementById("chat-status");
    var turnCounterEl = document.getElementById("chat-turn-counter");

    var conversationId = null;
    var turnCap = null;
    var turnCount = 0;

    function setStatus(text, isError) {
      if (!statusEl) {
        return;
      }
      statusEl.textContent = text || "";
      statusEl.classList.toggle("chat-status-error", !!isError);
    }

    // Visible running usage indicator ("Turn 4 of 20") -- turnCount is
    // incremented client-side once per completed ("done") turn, matching
    // the server's own chat_state.turn_count definition (one full
    // user-message-to-assistant-response cycle). The server is still the
    // real gate (see the 409 handling below); this is display only.
    function renderTurnCounter() {
      if (!turnCounterEl || turnCap === null) {
        return;
      }
      turnCounterEl.textContent = "Turn " + turnCount + " of " + turnCap;
    }

    function addBubble(role, text) {
      var bubble = document.createElement("div");
      bubble.className = "chat-bubble chat-bubble-" + role;
      bubble.textContent = text;
      transcript.appendChild(bubble);
      transcript.scrollTop = transcript.scrollHeight;
      return bubble;
    }

    function setInputEnabled(enabled) {
      input.disabled = !enabled;
      sendButton.disabled = !enabled;
    }

    // If a turn's propose_moves tool actually staged anything, show an
    // "Approve these N" action right under the assistant's reply -- POSTs
    // the exact same JSON entry_ids shape /queue/bulk-approve already
    // accepts (queue.html's own bulk-action bar uses this same route),
    // never a new approval code path. See chat-propose-and-approve's
    // design discussion §2.4.
    function addProposalActions(entryIds) {
      var wrap = document.createElement("div");
      wrap.className = "chat-bubble chat-proposal-actions";

      var label = document.createElement("span");
      label.textContent =
        entryIds.length + (entryIds.length === 1 ? " move proposed. " : " moves proposed. ");
      wrap.appendChild(label);

      var approveButton = document.createElement("button");
      approveButton.type = "button";
      approveButton.textContent = "Approve these " + entryIds.length;
      approveButton.setAttribute("data-intent", "primary");
      wrap.appendChild(approveButton);

      approveButton.addEventListener("click", function () {
        approveButton.disabled = true;
        fetch("/queue/bulk-approve", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ entry_ids: entryIds }),
        })
          .then(function (resp) {
            if (!resp.ok) {
              throw new Error("approve failed (HTTP " + resp.status + ")");
            }
            return resp.json();
          })
          .then(function (payload) {
            label.textContent = "Approved " + payload.count + ".";
            approveButton.remove();
          })
          .catch(function (err) {
            approveButton.disabled = false;
            setStatus(err.message, true);
          });
      });

      transcript.appendChild(wrap);
      transcript.scrollTop = transcript.scrollHeight;
    }

    // Create the conversation as soon as the page is ready -- a fresh
    // conversation_id per page load, matching the "in-memory, ephemeral"
    // design (chat/state.py never persists across a process restart, so
    // there is nothing meaningful to resume across page loads either).
    fetch("/chat/new", { method: "POST", headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (payload) {
        conversationId = payload.conversation_id;
        turnCap = payload.turn_cap;
        turnCount = 0;
        renderTurnCounter();
      })
      .catch(function (err) {
        setStatus("Could not start a conversation: " + err.message, true);
      });

    function pollTurn(jobId, assistantBubble) {
      fetch("/chat/status/" + jobId, { headers: { Accept: "application/json" } })
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("status check failed (HTTP " + resp.status + ")");
          }
          return resp.json();
        })
        .then(function (payload) {
          if (payload.status === "running") {
            if (payload.partial) {
              assistantBubble.textContent = payload.partial;
              transcript.scrollTop = transcript.scrollHeight;
            }
            window.setTimeout(function () {
              pollTurn(jobId, assistantBubble);
            }, POLL_INTERVAL_MS);
            return;
          }
          if (payload.status === "done") {
            assistantBubble.textContent = payload.result.text;
            transcript.scrollTop = transcript.scrollHeight;
            if (payload.result.staged_entry_ids && payload.result.staged_entry_ids.length > 0) {
              addProposalActions(payload.result.staged_entry_ids);
            }
            turnCount += 1;
            renderTurnCounter();
            if (turnCap !== null && turnCount >= turnCap) {
              setStatus("Conversation limit reached -- start a new one.", true);
              setInputEnabled(false);
            } else {
              setStatus("");
              setInputEnabled(true);
              input.focus();
            }
            return;
          }
          // payload.status === "error"
          assistantBubble.textContent = "(something went wrong)";
          assistantBubble.classList.add("chat-bubble-error");
          setStatus(payload.error || "unknown error", true);
          setInputEnabled(true);
        })
        .catch(function (err) {
          setStatus(err.message, true);
          setInputEnabled(true);
        });
    }

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      var message = input.value.trim();
      if (!message || !conversationId) {
        return;
      }

      addBubble("user", message);
      input.value = "";
      setInputEnabled(false);
      setStatus("Thinking…");
      var assistantBubble = addBubble("assistant", "");

      fetch("/chat/" + conversationId + "/message", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ message: message }),
      })
        .then(function (resp) {
          return resp.json().then(function (payload) {
            return { ok: resp.ok, status: resp.status, payload: payload };
          });
        })
        .then(function (result) {
          if (!result.ok) {
            // A 409 here is the server's own turn-cap gate (see
            // chat_message's docstring) -- surfaced with its real message,
            // never a generic "failed to send". The input stays disabled
            // in this case (there is nothing more this conversation can
            // do); any other non-ok status re-enables it so the user can
            // retry.
            assistantBubble.remove();
            var isCapped = result.status === 409;
            setStatus(result.payload.error || "could not send message", true);
            setInputEnabled(!isCapped);
            return;
          }
          pollTurn(result.payload.job_id, assistantBubble);
        })
        .catch(function (err) {
          assistantBubble.textContent = "(failed to send)";
          assistantBubble.classList.add("chat-bubble-error");
          setStatus(err.message, true);
          setInputEnabled(true);
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
