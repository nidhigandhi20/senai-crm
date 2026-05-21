import { useMemo, useState } from 'react';
import InboxToolbar from '../components/inbox/InboxToolbar';
import InboxTable from '../components/inbox/InboxTable';
import useDashboardStats from '../hooks/useDashboardStats';

const mockEmails = [];

const MissionControlInbox = () => {
  const [activeTab, setActiveTab] = useState('All');
  const [search, setSearch] = useState('');

  useDashboardStats();

  const filtered = useMemo(() => {
    return mockEmails.filter((e) => {
      const matchesSearch =
        e.subject?.toLowerCase().includes(search.toLowerCase()) ||
        e.body?.toLowerCase().includes(search.toLowerCase());

      if (activeTab === 'Needs Human') return e.requires_human && matchesSearch;
      if (activeTab === 'Spam') return e.category === 'Spam' && matchesSearch;
      if (activeTab === 'Escalated') return e.status === 'Escalated' && matchesSearch;
      if (activeTab === 'Auto-Replied') return e.status === 'Replied' && matchesSearch;

      return matchesSearch;
    });
  }, [activeTab, search]);

  return (
    <div className="p-6">
      <h1 className="text-3xl font-bold mb-6">Mission Control Inbox</h1>

      <InboxToolbar
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        search={search}
        setSearch={setSearch}
      />

      <InboxTable emails={filtered} onSelect={(e) => console.log(e)} />
    </div>
  );
};

export default MissionControlInbox;