import React from 'react'
import xaiLogo from '../assets/ai-providers/xai.png'

type Props = {
  className?: string
}

/** xAI / Grok brand mark on analysis cards. */
export const AiProviderLogo: React.FC<Props> = ({ className = 'h-9 w-9' }) => {
  const blend = 'mix-blend-screen'
  return (
    <img
      src={xaiLogo}
      alt=""
      title="Grok"
      className={`shrink-0 object-contain ${blend} ${className}`}
    />
  )
}
