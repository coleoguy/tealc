/* Click handler for the two sidebar pseudo-element links.

   Visuals are CSS pseudo-elements on #root (#root::after = COMMAND CENTER,
   #root::before = RESTART TEALC). Pseudo-elements survive Chainlit's React
   reconciliation but can't have href / can't receive their own click events,
   so we listen on `window` (capture phase, harder for React's synthetic
   event system to swallow) and route by viewport-coordinate hit-test. */
(function () {
  // Bounding boxes must match the CSS rules. Each is left:24, width:200,
  // padding:4px 0, font-size:11px → roughly 26px tall.
  var COMMAND_CENTER = { left: 24, top: 312, width: 200, height: 26 };
  var RESTART        = { left: 24, top: 344, width: 200, height: 26 };

  function hit(e, b) {
    return e.clientX >= b.left && e.clientX <= b.left + b.width
        && e.clientY >= b.top  && e.clientY <= b.top  + b.height;
  }

  window.addEventListener('click', function (e) {
    if (hit(e, COMMAND_CENTER)) {
      e.preventDefault();
      e.stopPropagation();
      window.open('http://localhost:8001', '_blank', 'noopener');
      return;
    }
    if (hit(e, RESTART)) {
      e.preventDefault();
      e.stopPropagation();
      var ok = window.confirm(
        'Restart Tealc?\n\n'
        + 'This will kill the chat (your current session ends) and the '
        + 'background scheduler, then respawn both. ~10–15 s of downtime.'
      );
      if (ok) {
        // Open the status page in a new tab so it survives the chat dying.
        window.open('http://localhost:8001/restart', '_blank', 'noopener');
      }
      return;
    }
  }, true /* capture */);
})();
