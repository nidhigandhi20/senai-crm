const tabs = [
  'All',
  'Needs Human',
  'Auto-Replied',
  'Escalated',
  'Spam',
];

const InboxToolbar = ({ activeTab, setActiveTab, search, setSearch }) => {
  return (
    <div className="flex flex-col gap-4 mb-4">
      <input
        placeholder="Search emails..."
        className="border rounded px-3 py-2"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />

      <div className="flex gap-2 flex-wrap">
        {tabs.map((tab) => (
          <button
            key={tab}
            className={`px-4 py-2 rounded ${activeTab === tab ? 'bg-blue-600 text-white' : 'bg-gray-200'}`}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>
    </div>
  );
};

export default InboxToolbar;