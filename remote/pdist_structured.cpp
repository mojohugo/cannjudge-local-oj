// Trusted, bounded synthetic family. Reuse the unchanged reference's dtype
// conversion. The renamed original main is never called; crosschecks execute a
// separately compiled, unmodified cpu_data.cpp (its main has implicit return 0).
#define main pdist_coordinate_reference_main
#include "cpu_data.cpp"
#undef main
#include <array>

static std::array<int,2> coefficients(size_t row) {
    // Adjacent duplicates, adjacent neighbours, extremes, zeros, varied amplitudes.
    if (row % 17 == 1) --row;
    switch (row % 32) {
        case 2: return {0,0};
        case 3: return {1,0};
        case 4: return {1,1};
        case 5: return {-128,128};
        case 6: return {128,-128};
        default: return {int((row*73+19)%257)-128, int((row*151+7)%257)-128};
    }
}

static int mask(size_t col, size_t cols, int layout) {
    // Interleaved, split and sparse block layouts exercise distinct column tails.
    if (layout == 0) return int(col%2);
    if (layout == 1) return col < cols/2 ? 0 : 1;
    return int((col/32 + col%3)%3);
}

int main(int argc, char** argv) {
    try {
        if (argc != 8 || std::string(argv[1]) != "structured")
            throw std::runtime_error("structured prefix dtype rows cols scale_exponent layout");
        const std::string prefix=argv[2];
        const int dtype=std::stoi(argv[3]), exponent=std::stoi(argv[6]), layout=std::stoi(argv[7]);
        const size_t rows=std::stoull(argv[4]), cols=std::stoull(argv[5]);
        if (dtype<0 || dtype>2 || rows<2 || rows>4097 || cols<1 || cols>16385
                || (exponent!=-10 && exponent!=-4 && exponent!=0) || layout<0 || layout>2)
            throw std::runtime_error("structured family bounds");
        size_t counts[2]={0,0};
        for(size_t c=0;c<cols;++c) { int m=mask(c,cols,layout); if(m<2)++counts[m]; }
        std::vector<float> x(rows*cols);
        for(size_t r=0;r<rows;++r) {
            auto ab=coefficients(r);
            for(size_t c=0;c<cols;++c) {
                int m=mask(c,cols,layout);
                x[r*cols+c]=m<2?std::ldexp(float(ab[m]),exponent):0.f;
            }
        }
        save(prefix+"_x.bin",x,dtype);
        // Every input is exact in all three dtypes. Integer squared sums are
        // <= 16385*256^2 < 2^31; scaling by 2^(2*exponent) is exact. All positive
        // coordinate partial sums are exact in FP64, regardless of reduction order.
        std::vector<double> y(rows*(rows-1)/2);
        for(size_t i=0;i<rows;++i) {
            auto a=coefficients(i); const size_t base=i*(2*rows-i-1)/2;
            for(size_t j=i+1;j<rows;++j) {
                auto b=coefficients(j);
                const int64_t da=a[0]-b[0], db=a[1]-b[1];
                const uint64_t sum=counts[0]*uint64_t(da*da)+counts[1]*uint64_t(db*db);
                y[base+j-i-1]=std::sqrt(std::ldexp(double(sum),2*exponent));
            }
        }
        save(prefix+"_y.bin",y,dtype);
        std::cout << "{\"k1\":" << counts[0] << ",\"k2\":" << counts[1] << "}\n";
        return 0;
    } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
