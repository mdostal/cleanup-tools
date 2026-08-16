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

    var conversationId = null;

    function setStatus(text, isError) {
      if (!statusEl) {
        return;
      }
      statusEl.textContent = text || "";
      statusEl.classList.toggle("chat-status-error", !!isError);
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
            setStatus("");
            setInputEnabled(true);
            input.focus();
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
          if (!resp.ok) {
            throw new Error("could not send message (HTTP " + resp.status + ")");
          }
          return resp.json();
        })
        .then(function (payload) {
          pollTurn(payload.job_id, assistantBubble);
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
