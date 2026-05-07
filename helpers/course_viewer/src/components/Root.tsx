import { Outlet } from '@tanstack/react-router'
import { Sidebar } from './Sidebar'
import { getSections } from '../lessonsStore'

export function Root() {
  const sections = getSections()
  return (
    <div className="flex h-screen overflow-hidden bg-gray-950 text-gray-100">
      <Sidebar sections={sections} />
      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
