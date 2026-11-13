/**
 * The active string table, chosen once when the app loads.
 *
 * Order of preference: the rider's saved choice, then the phone's language
 * settings, then English. A language whose `ready` flag is false is never
 * chosen and never offered, so an untranslated file can sit in the repository
 * without riders seeing it.
 *
 * To add a language: copy en.ts to xx.ts, type it `export const xx: Strings`
 * so the compiler checks every key and function signature, translate it, add
 * it to LOCALES below, and set its `ready` flag once a speaker has reviewed it.
 * Shona (sn.ts) is set up this way and waiting for its text.
 */
import { en } from './en'
import { sn, snReady } from './sn'

export type Strings = typeof en
export type Locale = 'en' | 'sn'

interface LocaleEntry {
  code: Locale
  /** The language's name in itself, as riders would look for it. */
  name: string
  strings: Strings
  ready: boolean
}

const LOCALES: readonly LocaleEntry[] = [
  { code: 'en', name: 'English', strings: en, ready: true },
  { code: 'sn', name: 'ChiShona', strings: sn, ready: snReady },
]

const LOCALE_KEY = 'kombi-capture:locale'

/** Languages a rider can choose. The switch only appears when there are two or more. */
export const availableLocales: { code: Locale; name: string }[] = LOCALES.filter(
  (entry) => entry.ready,
).map(({ code, name }) => ({ code, name }))

function savedLocale(): string | null {
  try {
    return localStorage.getItem(LOCALE_KEY)
  } catch {
    return null
  }
}

function pickLocale(): LocaleEntry {
  const ready = LOCALES.filter((entry) => entry.ready)
  // "sn-ZW" and "sn" both mean Shona; "en-GB" and "en" both mean English.
  const match = (tag: string | null | undefined): LocaleEntry | undefined =>
    tag ? ready.find((entry) => tag.toLowerCase().split('-')[0] === entry.code) : undefined
  const preferred = navigator.languages?.length ? navigator.languages : [navigator.language]
  return match(savedLocale()) ?? preferred.map(match).find((entry) => entry !== undefined) ?? LOCALES[0]
}

const active = pickLocale()

export const locale: Locale = active.code

export const t: Strings = active.strings

/** Switch language. Strings are fixed when the app loads, so this reloads the page. */
export function setLocale(code: Locale): void {
  try {
    localStorage.setItem(LOCALE_KEY, code)
  } catch {
    // Not remembered; the reload still picks the phone's language.
  }
  window.location.reload()
}
