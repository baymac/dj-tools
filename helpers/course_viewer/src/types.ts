export interface Attachment {
  name: string
  file: string   // relative path under public/, e.g. "files/123/kit.zip"
  size: string   // display string e.g. "20.5 MB"
}

export interface Subtitle {
  label: string  // user-visible track name
  file: string   // relative path, e.g. "subtitles/<id>/0.vtt"
  lang: string   // BCP-47 lang code for srclang
  default?: boolean
}

export type LessonType =
  | 'locked'
  | 'video_circle'
  | 'video_dyntube'
  | 'quiz'
  | 'exercise'
  | 'guide'
  | 'content'
  | 'unknown'

export interface Lesson {
  id: string
  sectionTitle: string
  sectionIndex: number
  lessonIndex: number
  title: string
  url: string
  type?: LessonType
  extracted?: boolean
  completed?: boolean      // platform-side completion status (we clicked "Complete lesson")
  videoFile: string | null
  videoUrl?: string | null
  thumbFile?: string | null
  contentHtml: string
  attachments: Attachment[]
  subtitles?: Subtitle[]
  quizFile?: string | null
  error?: string | null
}

export interface Section {
  index: number
  title: string
  lessons: Lesson[]
}
