/* Shared searchable country picker for the site editor and branch copies. */
(() => {
  const catalog = [...document.querySelectorAll('#country-options option')].map(option => ({
    name: option.value,
    aliases: (option.dataset.search || '').split('|')
  }));
  const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase().replace(/[_-]/g, ' ').replace(/^the /, '').trim();
  let opened = null, sequence = 0;
  const attached = new WeakSet();

  function attach(input) {
    if (attached.has(input)) return;
    attached.add(input);
    input.removeAttribute('list');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    if (!input.hasAttribute('aria-label')) input.setAttribute('aria-label', 'Country');
    const wrapper = document.createElement('span');
    wrapper.className = 'country-picker';
    input.before(wrapper);
    wrapper.append(input);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'country-toggle';
    toggle.setAttribute('aria-label', 'Show countries');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.textContent = '▾';
    const list = document.createElement('div');
    list.className = 'country-menu';
    list.id = `country-list-${++sequence}`;
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', 'Countries');
    // The top layer keeps the list visible outside a scrolling table or dialog.
    list.setAttribute('popover', 'manual');
    list.hidden = true;
    input.setAttribute('aria-controls', list.id);
    toggle.setAttribute('aria-controls', list.id);
    wrapper.append(toggle, list);
    let matches = [], active = -1, selecting = false;

    function position() {
      if (!input.isConnected || !input.getClientRects().length) return close();
      const rect = input.getBoundingClientRect();
      const below = window.innerHeight - rect.bottom - 12;
      const above = rect.top - 12;
      const height = Math.min(250, Math.max(below, above));
      list.style.width = `${Math.min(Math.max(rect.width, 220), window.innerWidth - 24)}px`;
      list.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - parseFloat(list.style.width) - 12))}px`;
      list.style.maxHeight = `${height}px`;
      const actualHeight = Math.min(list.scrollHeight, height);
      list.style.top = `${below >= actualHeight ? rect.bottom + 4 : Math.max(8, rect.top - actualHeight - 4)}px`;
    }

    function highlight(index) {
      active = index;
      [...list.querySelectorAll('[role=option]')].forEach((option, i) => {
        option.setAttribute('aria-selected', String(i === active));
      });
      const option = list.querySelectorAll('[role=option]')[active];
      if (option) {
        input.setAttribute('aria-activedescendant', option.id);
        option.scrollIntoView({block: 'nearest'});
      } else input.removeAttribute('aria-activedescendant');
    }

    function close() {
      if (list.matches(':popover-open')) list.hidePopover();
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      if (opened?.input === input) opened = null;
    }

    function choose(country) {
      selecting = true;
      input.value = country.name;
      close();
      input.focus({preventScroll: true});
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
      selecting = false;
    }

    function show(all = false) {
      if (opened?.input !== input) opened?.close();
      const query = all ? '' : normalize(input.value);
      matches = catalog.filter(country => !query || [country.name, ...country.aliases]
        .some(value => normalize(value).includes(query)));
      const rank = country => {
        const values = [country.name, ...country.aliases].map(normalize);
        return values.includes(query) ? 0 : values.some(value => value.startsWith(query)) ? 1 : 2;
      };
      matches.sort((a, b) => rank(a) - rank(b) || normalize(a.name).localeCompare(normalize(b.name)));
      list.replaceChildren();
      for (const [index, country] of matches.entries()) {
        const option = document.createElement('div');
        option.className = 'country-option';
        option.id = `${list.id}-${index}`;
        option.setAttribute('role', 'option');
        option.textContent = country.name;
        option.addEventListener('pointerdown', event => event.preventDefault());
        option.addEventListener('click', event => {event.preventDefault(); choose(country);});
        list.append(option);
      }
      if (!matches.length) {
        const empty = document.createElement('div');
        empty.className = 'country-no-match';
        empty.setAttribute('role', 'status');
        empty.textContent = 'No matching country. Try another name.';
        list.append(empty);
      }
      list.hidden = false;
      if (list.showPopover && !list.matches(':popover-open')) list.showPopover();
      opened = {input, wrapper, list, close, position};
      input.setAttribute('aria-expanded', 'true');
      toggle.setAttribute('aria-expanded', 'true');
      list.scrollTop = 0;
      position();
      highlight(matches.length ? 0 : -1);
    }

    input.addEventListener('focus', () => {if (!selecting) show();});
    input.addEventListener('click', () => {if (!opened) show();});
    input.addEventListener('input', () => {if (!selecting) show();});
    input.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (opened?.input !== input) return show();
        if (matches.length) highlight((active + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length);
      } else if (event.key === 'Enter' && opened?.input === input) {
        event.preventDefault();
        if (active >= 0) choose(matches[active]);
      } else if (event.key === 'Escape' && opened?.input === input) {
        event.preventDefault();
        event.stopPropagation();
        close();
      } else if (event.key === 'Tab') close();
    });
    toggle.addEventListener('pointerdown', event => event.preventDefault());
    toggle.addEventListener('click', event => {
      event.preventDefault();
      if (opened?.input === input) close();
      else {selecting = true; input.focus({preventScroll: true}); selecting = false; show(true);}
    });
    wrapper.addEventListener('focusout', event => {
      if (!wrapper.contains(event.relatedTarget)) close();
    });
  }

  document.addEventListener('pointerdown', event => {
    if (opened && !opened.wrapper.contains(event.target)) opened.close();
  });
  document.addEventListener('close', event => {
    if (opened && event.target.contains(opened.input)) opened.close();
  }, true);
  window.addEventListener('resize', () => opened?.position());
  document.addEventListener('scroll', event => {
    if (opened && event.target !== opened.list) opened.position();
  }, true);
  globalThis.CountryPicker = {attach, close: () => opened?.close()};
  document.querySelectorAll('input[list="country-options"]').forEach(attach);
})();
