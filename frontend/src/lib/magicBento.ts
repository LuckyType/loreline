// Cursor-driven card glow, proximity spotlight and hover star particles,
// adapted from svelte-bits' Magic Bento
// (https://sveltebits.xyz/components/magic-bento), re-tuned to the app accent
// and implemented without gsap (Web Animations API instead). The demo's tilt,
// magnetism and click ripple are deliberately left out - they fight with
// form-heavy cards.

const SPOTLIGHT_RADIUS = 300
const PROXIMITY = SPOTLIGHT_RADIUS * 0.5
const FADE_DISTANCE = SPOTLIGHT_RADIUS * 0.75
const MOBILE_BREAKPOINT = 768
const PARTICLE_COUNT = 12
// --primary (indigo) as r,g,b - CSS can't feed an oklch token into the
// rgba()-based gradients the effect uses.
const GLOW_RGB = '99, 102, 241'

function spawnParticle(card: HTMLElement, width: number, height: number): HTMLDivElement {
	const p = document.createElement('div')
	p.className = 'magic-particle'
	p.style.left = `${Math.random() * width}px`
	p.style.top = `${Math.random() * height}px`
	card.appendChild(p)
	p.animate(
		[
			{ transform: 'scale(0)', opacity: 0 },
			{ transform: 'scale(1)', opacity: 1 },
		],
		{ duration: 300, easing: 'cubic-bezier(0.34, 1.56, 0.64, 1)', fill: 'forwards' },
	)
	p.animate(
		[
			{ transform: 'translate(0px, 0px) rotate(0deg)' },
			{
				transform: `translate(${(Math.random() - 0.5) * 100}px, ${(Math.random() - 0.5) * 100}px) rotate(${Math.random() * 360}deg)`,
			},
		],
		{
			duration: 2000 + Math.random() * 2000,
			direction: 'alternate',
			iterations: Number.POSITIVE_INFINITY,
			easing: 'linear',
			composite: 'add',
		},
	)
	p.animate([{ opacity: 1 }, { opacity: 0.3 }], {
		duration: 1500,
		delay: 300,
		direction: 'alternate',
		iterations: Number.POSITIVE_INFINITY,
		easing: 'ease-in-out',
	})
	return p
}

/** Start the effect document-wide; returns a cleanup function. */
export function initMagicBento(): () => void {
	if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
		return () => {}
	}

	const spotlight = document.createElement('div')
	spotlight.className = 'magic-spotlight'
	spotlight.style.background = `radial-gradient(circle, rgba(${GLOW_RGB}, 0.1) 0%, rgba(${GLOW_RGB}, 0.05) 15%, rgba(${GLOW_RGB}, 0.03) 25%, rgba(${GLOW_RGB}, 0.015) 40%, transparent 70%)`
	document.body.appendChild(spotlight)

	const cards = () => document.querySelectorAll<HTMLElement>('[data-slot="card"]')

	let raf = 0
	const onMove = (e: MouseEvent) => {
		if (raf) return
		raf = requestAnimationFrame(() => {
			raf = 0
			if (window.innerWidth <= MOBILE_BREAKPOINT) return
			let minDistance = Number.POSITIVE_INFINITY
			for (const card of cards()) {
				const r = card.getBoundingClientRect()
				const center = Math.hypot(
					e.clientX - (r.left + r.width / 2),
					e.clientY - (r.top + r.height / 2),
				)
				const distance = Math.max(0, center - Math.max(r.width, r.height) / 2)
				minDistance = Math.min(minDistance, distance)
				let glow = 0
				if (distance <= PROXIMITY) glow = 1
				else if (distance <= FADE_DISTANCE) {
					glow = (FADE_DISTANCE - distance) / (FADE_DISTANCE - PROXIMITY)
				}
				card.style.setProperty('--glow-x', `${((e.clientX - r.left) / r.width) * 100}%`)
				card.style.setProperty('--glow-y', `${((e.clientY - r.top) / r.height) * 100}%`)
				card.style.setProperty('--glow-intensity', String(glow))
				// The demo's fixed 300px pool suits small bento tiles; our cards run
				// wide, so scale the pool with the card or its corners never light.
				card.style.setProperty(
					'--glow-radius',
					`${Math.max(SPOTLIGHT_RADIUS, Math.max(r.width, r.height))}px`,
				)
			}
			spotlight.style.left = `${e.clientX}px`
			spotlight.style.top = `${e.clientY}px`
			const opacity =
				minDistance <= PROXIMITY
					? 0.8
					: minDistance <= FADE_DISTANCE
						? ((FADE_DISTANCE - minDistance) / (FADE_DISTANCE - PROXIMITY)) * 0.8
						: 0
			spotlight.style.opacity = String(opacity)
		})
	}

	const onLeave = () => {
		spotlight.style.opacity = '0'
		for (const card of cards()) card.style.setProperty('--glow-intensity', '0')
	}

	// Star particles on the hovered card, tracked via delegation so cards
	// mounted by later route changes need no wiring of their own.
	let hoveredCard: HTMLElement | null = null
	let starTimeouts: number[] = []
	let stars: HTMLDivElement[] = []

	const clearStars = () => {
		for (const t of starTimeouts) clearTimeout(t)
		starTimeouts = []
		for (const p of stars) {
			const exit = p.animate(
				[
					{ transform: 'scale(1)', opacity: 1 },
					{ transform: 'scale(0)', opacity: 0 },
				],
				{ duration: 300, easing: 'ease-in', fill: 'forwards' },
			)
			exit.onfinish = () => p.remove()
		}
		stars = []
	}

	const onOver = (e: MouseEvent) => {
		const card =
			e.target instanceof Element
				? (e.target.closest('[data-slot="card"]') as HTMLElement | null)
				: null
		if (card === hoveredCard) return
		clearStars()
		hoveredCard = card
		if (!card || window.innerWidth <= MOBILE_BREAKPOINT) return
		const { width, height } = card.getBoundingClientRect()
		for (let i = 0; i < PARTICLE_COUNT; i++) {
			const t = window.setTimeout(() => {
				if (hoveredCard !== card) return
				stars.push(spawnParticle(card, width, height))
			}, i * 100)
			starTimeouts.push(t)
		}
	}

	document.addEventListener('mousemove', onMove)
	document.addEventListener('mouseover', onOver)
	document.documentElement.addEventListener('mouseleave', onLeave)
	return () => {
		if (raf) cancelAnimationFrame(raf)
		document.removeEventListener('mousemove', onMove)
		document.removeEventListener('mouseover', onOver)
		document.documentElement.removeEventListener('mouseleave', onLeave)
		clearStars()
		spotlight.remove()
	}
}
