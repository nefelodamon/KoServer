'use strict';

/**
 * Toggle the expanded detail panel on a character card.
 * @param {HTMLButtonElement} btn
 */
function toggleExpand(btn) {
  const detail = btn.nextElementSibling;
  if (!detail) return;
  const isHidden = detail.classList.toggle('hidden');
  btn.textContent = isHidden ? 'Show more' : 'Show less';
}

/**
 * Reveal a spoiler-blurred portrait and un-blur the character name.
 * @param {HTMLElement} overlay
 */
function revealSpoiler(overlay) {
  const wrap = overlay.closest('.character-portrait-wrap');
  const card = overlay.closest('.character-card');
  if (wrap) wrap.classList.remove('spoiler-blur');
  if (card) {
    const name = card.querySelector('.spoiler-text');
    if (name) name.classList.remove('spoiler-text');
  }
  overlay.remove();
}
