import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { homedir } from 'os'
import { resolve } from 'path'

// All downloaded course data lives in ~/Music/dj-tools/course/
// Vite serves this directory as static assets so fetch('/lessons.json') etc. just work.
const courseDir = resolve(homedir(), 'Music', 'dj-tools', 'course')

export default defineConfig({
  plugins: [react()],
  publicDir: courseDir,
  server: {
    open: true,
  },
})
