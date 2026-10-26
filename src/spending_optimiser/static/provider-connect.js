/* Provider sign-in window for the Connect buttons. Sends no credentials, codes or messages
   between windows: the app only asks its own server whether the attempt has finished. */
(function () {
  "use strict";

  var WINDOW_NAME = "provider-auth";
  var FEATURES = "popup,width=520,height=720";
  var INTERVAL = 1500;
  var TIMEOUT = 11 * 60 * 1000;

  function visible(element, show) {
    if (element) {
      element.hidden = !show;
    }
  }

  function watch(status, baseline, popup) {
    var started = Date.now();
    function again() {
      if (Date.now() - started < TIMEOUT) {
        window.setTimeout(poll, INTERVAL);
      }
    }
    function poll() {
      fetch(status, { credentials: "same-origin", cache: "no-store" })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (data) {
          if (data && data.state !== "pending" && data.attempt !== baseline) {
            try {
              if (popup) {
                popup.close();
              }
            } catch (ignored) {
              /* The provider page may have severed the window; the user can close it. */
            }
            window.location.reload();
          } else {
            again();
          }
        })
        .catch(again);
    }
    poll();
  }

  function connectForm(form) {
    var name = form.dataset.providerConnect;
    var panel = document.querySelector('[data-auth-wait="' + name + '"]');
    var blocked = document.querySelector('[data-auth-blocked="' + name + '"]');
    var display = form.querySelector('input[name="display"]');
    var watching = false;

    form.addEventListener("submit", function (event) {
      if (form.target !== WINDOW_NAME) {
        return;
      }
      var popup = window.open("", WINDOW_NAME, FEATURES);
      if (!popup) {
        event.preventDefault();
        visible(blocked, true);
        var action = blocked && blocked.querySelector("button");
        if (action) {
          action.focus();
        }
        return;
      }
      visible(blocked, false);
      visible(panel, true);
      if (!watching) {
        watching = true;
        watch(form.dataset.status, Number(form.dataset.attempt), popup);
      }
    });

    var fallback = blocked && blocked.querySelector("button");
    if (fallback) {
      fallback.addEventListener("click", function () {
        // Explicit continuation without a second window; the server redirects back.
        form.removeAttribute("target");
        display.value = "window";
        form.submit();
      });
    }
  }

  var forms = document.querySelectorAll("form[data-provider-connect]");
  for (var index = 0; index < forms.length; index += 1) {
    connectForm(forms[index]);
  }

  // A server-rendered waiting panel (page reloaded mid sign-in) resumes watching.
  var resume = document.querySelector("[data-auth-resume]");
  if (resume) {
    watch(resume.dataset.status, -1, null);
  }

  // The sign-in window's result page closes itself where the browser allows it.
  if (document.querySelector("[data-auth-result]")) {
    var close = document.querySelector("[data-close-window]");
    if (close) {
      close.hidden = false;
      close.addEventListener("click", function () {
        window.close();
      });
    }
    window.setTimeout(function () {
      window.close();
    }, 1800);
  }
})();
