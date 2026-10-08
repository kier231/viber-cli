'use strict';
(() => {
  const trigger = document.getElementById('workspace-switch');
  const menu = document.getElementById('workspace-menu');
  const close = () => { menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); };
  trigger.addEventListener('click', () => {
    menu.hidden = !menu.hidden;
    trigger.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', event => { if (!event.target.closest('.workspace-picker')) close(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !menu.hidden) { close(); trigger.focus(); } });
})();
