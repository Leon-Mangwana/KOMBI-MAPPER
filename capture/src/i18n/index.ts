/**
 * The active string table.
 *
 * To add a language: copy en.ts to, say, sn.ts, translate it, and type it
 * `export const sn: Strings = { … }` so the compiler checks every key and
 * function signature. Then choose between them here, for example by
 * `navigator.language` or a saved setting.
 */
import { en } from './en'

export type Strings = typeof en

export const t: Strings = en
