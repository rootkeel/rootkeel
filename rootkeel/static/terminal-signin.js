// SPDX-License-Identifier: AGPL-3.0-only
"use strict";
// The broker's sign-in page for one grant, and its page for a grant that has ended. The form posts the session
// credential that rt grant prints. The code that rt grant --qr shows opens the grant's URL with the credential in
// the fragment (#token=...), which browsers never send to the server: this takes the fragment out of the address bar
// and the tab's history first, then signs in with it, so a phone that scans the code has nothing to type. A link
// opened again in this tab only changes the fragment, without loading the page, so it is handled the same way.
(() => {
  const form = document.querySelector('form'), status = document.querySelector('[role=status]');
  const show = text => { if (status) status.textContent = text; };
  const refused = 'Grant invalid or ended. Request a new session.';
  // Signs in, then reloads into the terminal; otherwise returns false.
  const signIn = async credential => {
    try {
      const response = await fetch('./command/login', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({auth_token: credential})});
      if (!response.ok) return false;
    } catch {
      return false;
    }
    location.reload();
    return true;
  };
  if (form) {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const input = form.querySelector('input'), credential = input.value;
      input.value = '';
      show(await signIn(credential) ? '' : refused);
    });
  }
  const link = () => {
    const token = new URLSearchParams(location.hash.slice(1)).get('token');
    if (location.hash) history.replaceState(history.state, '', location.pathname + location.search);
    if (token === null) return;
    const scanned = 'The scanned code did not sign in. ';
    if (!form) {
      show(scanned + refused);
      return;
    }
    show('Signing in…');
    signIn(token).then(signedIn => {
      if (!signedIn) show(scanned + refused);
    });
  };
  addEventListener('hashchange', link);
  link();
})();
