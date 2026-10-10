`default_nettype none
module cache_storage_pair_probe #(
  parameter int I_BYTES = 1024,
  parameter int D_BYTES = 1024,
  parameter int WAYS = 2,
  parameter int BANK_WORDS = 8,
  localparam int DATA_BITS = BANK_WORDS * 32
) (
  input wire clk_i, rst_i,
  input wire [1:0] en_i, write_i, touch_i, way_i, valid_i, dirty_i,
  input wire [63:0] address_i,
  input wire [2*DATA_BITS-1:0] wdata_i,
  input wire [2*DATA_BITS/8-1:0] wmask_i,
  output wire [1:0] response_o, hit_o, dirty_o, way_o,
  output wire [63:0] victim_address_o,
  output wire [2*DATA_BITS-1:0] rdata_o
);
  for (genvar c = 0; c < 2; c++) begin : caches
    cache_storage_probe #(.BYTES(c == 0 ? I_BYTES : D_BYTES), .WAYS(WAYS),
      .BANK_WORDS(BANK_WORDS), .HAS_DIRTY(c == 1)) storage (
      .clk_i, .rst_i, .en_i(en_i[c]), .write_i(write_i[c]), .touch_i(touch_i[c]),
      .way_i(way_i[c]), .valid_i(valid_i[c]), .dirty_i(dirty_i[c]),
      .address_i(address_i[c*32 +: 32]), .wdata_i(wdata_i[c*DATA_BITS +: DATA_BITS]),
      .wmask_i(wmask_i[c*DATA_BITS/8 +: DATA_BITS/8]), .response_o(response_o[c]),
      .hit_o(hit_o[c]), .dirty_o(dirty_o[c]), .way_o(way_o[c]),
      .victim_address_o(victim_address_o[c*32 +: 32]), .rdata_o(rdata_o[c*DATA_BITS +: DATA_BITS])
    );
  end
endmodule

// Storage commands expose bank groups; refill, writeback and line assembly are external.
module cache_storage_probe #(
  parameter int BYTES = 1024,
  parameter int WAYS = 2,
  parameter int BANK_WORDS = 8,
  parameter bit HAS_DIRTY = 1,
  localparam int DATA_BITS = BANK_WORDS * 32
) (
  input wire clk_i, rst_i, en_i, write_i, touch_i, way_i, valid_i, dirty_i,
  input wire [31:0] address_i,
  input wire [DATA_BITS-1:0] wdata_i,
  input wire [DATA_BITS/8-1:0] wmask_i,
  output logic response_o, hit_o, dirty_o, way_o,
  output logic [31:0] victim_address_o,
  output logic [DATA_BITS-1:0] rdata_o
);
  localparam int SETS = BYTES / (32 * WAYS);
  localparam int INDEX_BITS = $clog2(SETS), TAG_BITS = 27 - INDEX_BITS;
  localparam int GROUPS = 8 / BANK_WORDS;
  localparam int ROW_BITS = $clog2(SETS * GROUPS);
  localparam int DATA_DEPTH = SETS * GROUPS < 16 ? 16 : SETS * GROUPS;
  localparam int TAG_DEPTH = SETS < 16 ? 16 : SETS;
  localparam int SLICE_BITS = BANK_WORDS == 8 ? 128 : 32;
  localparam int SLICES = DATA_BITS / SLICE_BITS;
  if (!(WAYS inside {1, 2}) || !(BANK_WORDS inside {1, 2, 8})
      || SETS < 8 || (SETS & (SETS - 1)) != 0 || BYTES != SETS * WAYS * 32) begin : illegal_geometry
    $error("unsupported storage pilot geometry");
  end

  wire [INDEX_BITS-1:0] index = address_i[5 +: INDEX_BITS];
  wire [TAG_BITS-1:0] tag = address_i[31:5+INDEX_BITS];
  wire [$clog2(DATA_DEPTH)-1:0] row = $clog2(DATA_DEPTH)'(address_i[2+$clog2(BANK_WORDS) +: ROW_BITS]);
  wire [$clog2(TAG_DEPTH)-1:0] tag_row = $clog2(TAG_DEPTH)'(index);
  logic [WAYS-1:0] valid_q [SETS], dirty_q [SETS];
  logic lru_q [SETS];
  logic [WAYS-1:0] read_valid_q, read_dirty_q;
  logic read_lru_q;
  logic [INDEX_BITS-1:0] read_index_q;
  logic [TAG_BITS-1:0] read_tag_q;
  wire [TAG_BITS-1:0] tags [WAYS];
  wire [DATA_BITS-1:0] data [WAYS];
  logic [WAYS-1:0] hits;
  logic selected;

  for (genvar w = 0; w < WAYS; w++) begin : ways
    wire access = en_i && !rst_i && (!write_i || way_i == 1'(w));
    dense_memory_1rw #(.DEPTH(TAG_DEPTH), .WIDTH(TAG_BITS), .MASKED(0)) tag_array (
      .clk_i, .en_i(access), .we_i(write_i), .addr_i(tag_row), .wdata_i(tag), .wmask_i(1'b1), .rdata_o(tags[w])
    );
    for (genvar s = 0; s < SLICES; s++) begin : slices
      dense_memory_1rw #(.DEPTH(DATA_DEPTH), .WIDTH(SLICE_BITS)) data_array (
        .clk_i, .en_i(access), .we_i(write_i), .addr_i(row),
        .wdata_i(wdata_i[s*SLICE_BITS +: SLICE_BITS]), .wmask_i(wmask_i[s*SLICE_BITS/8 +: SLICE_BITS/8]),
        .rdata_o(data[w][s*SLICE_BITS +: SLICE_BITS])
      );
    end
  end

  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      response_o <= 0;
      read_valid_q <= 0;
      read_dirty_q <= 0;
      read_lru_q <= 0;
      for (int s = 0; s < SETS; s++) begin
        valid_q[s] <= 0;
        dirty_q[s] <= 0;
        lru_q[s] <= 0;
      end
    end else begin
      response_o <= en_i && !write_i;
      if (en_i && !write_i) begin
        read_valid_q <= valid_q[index];
        read_dirty_q <= dirty_q[index];
        read_lru_q <= lru_q[index];
        read_index_q <= index;
        read_tag_q <= tag;
      end
      if (en_i && write_i) begin
        valid_q[index][way_i] <= valid_i;
        dirty_q[index][way_i] <= HAS_DIRTY && dirty_i;
      end
      if (touch_i && WAYS == 2) lru_q[index] <= !way_i;
    end
  end

  always_comb begin
    for (int w = 0; w < WAYS; w++) hits[w] = read_valid_q[w] && tags[w] == read_tag_q;
    selected = WAYS == 2 ? read_lru_q : 1'b0;
    for (int w = WAYS-1; w >= 0; w--) if (!read_valid_q[w]) selected = 1'(w);
    for (int w = WAYS-1; w >= 0; w--) if (hits[w]) selected = 1'(w);
    hit_o = response_o && |hits;
    way_o = response_o && selected;
    dirty_o = response_o && read_valid_q[selected] && read_dirty_q[selected];
    victim_address_o = 0;
    rdata_o = 0;
    if (response_o && read_valid_q[selected]) begin
      victim_address_o = {tags[selected], read_index_q, 5'd0};
      rdata_o = data[selected];
    end
  end
endmodule
`default_nettype wire
