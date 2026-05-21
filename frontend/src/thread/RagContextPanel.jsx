const RagContextPanel = ({ chunks }) => {
  return (
    <div className="bg-white rounded shadow p-4 mt-4">
      <h2 className="text-lg font-semibold mb-3">RAG Context</h2>

      <div className="space-y-3">
        {chunks?.map((chunk, index) => (
          <div key={index} className="border rounded p-3">
            <div className="flex justify-between mb-2">
              <span className="font-semibold">{chunk.source_doc}</span>
              <span>{chunk.similarity_score}</span>
            </div>

            <p className="text-sm text-gray-700">{chunk.chunk_text}</p>
          </div>
        ))}
      </div>
    </div>
  );
};

export default RagContextPanel;