import { create } from 'zustand';

const useDashboardStore = create((set) => ({
  selectedThread: null,
  selectedEmail: null,
  activeTab: 'All',
  search: '',

  setSelectedThread: (thread) => set({ selectedThread: thread }),
  setSelectedEmail: (email) => set({ selectedEmail: email }),
  setActiveTab: (tab) => set({ activeTab: tab }),
  setSearch: (search) => set({ search }),
}));

export default useDashboardStore;