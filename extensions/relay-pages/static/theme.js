// ?theme=dark|light overrides the system theme (ReaderPage.md). Loaded in <head> to avoid a flash.
(function () {
  var match = /[?&]theme=(dark|light)(?:&|$)/.exec(location.search);
  if (match) document.documentElement.setAttribute('data-theme', match[1]);
})();
