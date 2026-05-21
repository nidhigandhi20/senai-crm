const colorMap = {
  Positive: 'bg-green-500',
  Neutral: 'bg-yellow-500',
  Negative: 'bg-red-500',
  Critical: 'bg-red-700',
};

const Badge = ({ label }) => {
  return (
    <span className={`px-2 py-1 rounded text-xs text-white ${colorMap[label] || 'bg-gray-500'}`}>
      {label}
    </span>
  );
};

export default Badge;