interface CourseEntry {
  id: string
  name: string
  lessonCount: number
}

interface Props {
  courses: CourseEntry[]
  onSelect: (course: CourseEntry) => void
}

export function CourseSelector({ courses, onSelect }: Props) {
  return (
    <div className="min-h-screen bg-gray-950 flex flex-col items-center justify-center px-6 py-12">
      <h1 className="text-2xl font-bold text-white mb-2">Pete Tong DJ Academy</h1>
      <p className="text-sm text-gray-400 mb-10">Choose a course to view</p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 w-full max-w-2xl">
        {courses.map(course => (
          <button
            key={course.id}
            onClick={() => onSelect(course)}
            className="text-left rounded-xl border border-gray-800 bg-gray-900 hover:bg-gray-800 hover:border-gray-700 transition-colors p-5"
          >
            <p className="font-semibold text-white text-base leading-snug mb-2">{course.name}</p>
            <p className="text-xs text-gray-500">{course.lessonCount} lessons</p>
          </button>
        ))}
      </div>
    </div>
  )
}
