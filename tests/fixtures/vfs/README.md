# VFS classifier fixtures

- `captcha_real.html` is REAL: the Cloudflare "Waiting Room" page returned by
  `https://visa.vfsglobal.com/tur/en/fra/login`, fetched on 2026-11-11 with curl
  (Chrome user agent). It contains `challenge-platform`.
- `login.html`, `no_slots.html`, `waitlist.html`, `available.html`, `unknown.html`
  are SYNTHETIC. They reproduce publicly known VFS texts and the texts of the
  mock-vfs site; they do not prove the real VFS looks the same.
